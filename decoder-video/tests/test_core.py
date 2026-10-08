import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DECODER_DATA_DIR", tempfile.mkdtemp(prefix="decoder-test-"))

import pytest  # noqa: E402

from decoder import captions, cues, db  # noqa: E402
from decoder.csvdata import detect_x_type, parse_csv  # noqa: E402
from decoder.templates import get  # noqa: E402
from decoder.templates.line_split import divergence_x  # noqa: E402
from decoder.theme import load_theme  # noqa: E402

SAMPLE = (ROOT / "sample_data" / "canada_population_change.csv").read_text()


def words_from(text, gap=0.4):
    out, t = [], 0.0
    for i, w in enumerate(text.split()):
        out.append({"i": i, "text": w, "start": round(t, 3), "end": round(t + 0.3, 3), "prob": 1})
        t += gap
    return out


# ---------------------------------------------------------------- csv

def test_parse_sample_quarters():
    d = parse_csv(SAMPLE)
    assert d["x"]["type"] == "quarter"
    assert d["x"]["positions"][:2] == [2020.0, 2020.25]
    assert set(d["columns"]) == {"previous_estimate", "revised_estimate"}
    assert d["columns"]["previous_estimate"][-1] == -61900


@pytest.mark.parametrize("vals,kind", [
    (["2020Q1", "2020 Q2"], "quarter"), (["Q3 2021", "Q4 2021"], "quarter"),
    (["2020-01", "2020-02"], "month"), (["2020-01-15", "2021-06-30"], "date"),
    (["01-Jan-20", "01-Apr-21", "01-Oct-25"], "date"), (["Apr-21", "Jul-21"], "month"),
    (["April 2021", "May 2021"], "month"), (["4/1/2021", "12/31/2021"], "date"),
    (["2019", "2020"], "year"), (["1.5", "2"], "number"), (["Ontario", "Quebec"], "category"),
])
def test_detect_x_type(vals, kind):
    assert detect_x_type(vals) == kind


def test_spreadsheet_dates_become_positions():
    d = parse_csv("date,a\n01-Jan-21,1\n01-Apr-21,2\n01-Jul-21,3\n")
    assert d["x"]["type"] == "date" and d["x"]["values"][1] == "01-Apr-21"
    assert d["x"]["positions"][0] == 2021.0 and 2021.24 < d["x"]["positions"][1] < 2021.25


def test_parse_handles_gaps_and_formatting():
    d = parse_csv("year,a\n2019,\"1,200\"\n2020,..\n2021,−300\n")
    assert d["columns"]["a"] == [1200.0, None, -300.0]


def test_parse_rejects_no_numeric():
    with pytest.raises(ValueError):
        parse_csv("x,label\n1,a\n2,b\n")


# ---------------------------------------------------------------- templates

def test_cue_validation_and_defaults():
    data, theme = parse_csv(SAMPLE), load_theme("default")
    T = get("line_draw")
    cfg = T.default_config(data, theme)
    clean, errs = T.validate_cue("draw_line", {"series": "previous_estimate", "to": "2021-Q1"}, cfg, data)
    assert not errs and clean["duration"] == 2.0 and clean["to"] == "2021-Q1"
    _, errs = T.validate_cue("draw_line", {"series": "nope", "to": "1999-Q1"}, cfg, data)
    assert len(errs) == 2
    _, errs = T.validate_cue("split_line", {}, cfg, data)
    assert errs  # not a line_draw action


def test_line_split_detects_divergence_and_defaults_series():
    data, theme = parse_csv(SAMPLE), load_theme("default")
    assert divergence_x(data, "previous_estimate", "revised_estimate") == "2025-Q2"
    T = get("line_split")
    cfg = T.default_config(data, theme)
    assert cfg["options"]["split_from"] == "2025-Q2"
    clean, errs = T.validate_cue("split_line", {}, cfg, data)
    assert not errs and clean["series"] == "revised_estimate"
    _, errs = T.validate_cue("shade_gap", {"series": "previous_estimate"}, cfg, data)
    assert errs


def test_template_manifests_are_json():
    for name in ("line_draw", "line_split"):
        json.dumps(get(name).manifest())


# ---------------------------------------------------------------- captions

def test_caption_groups_respect_limits_and_punctuation():
    style = load_theme("default")["captions"]
    w = words_from("This is Decoder. Canada's population was growing fast before the pandemic.")
    groups = captions.group_words(w, style)
    for g in groups:
        assert 1 <= len(g["words"]) <= style["max_words"]
        assert len(g["text"]) <= style["max_chars"] + 4
    assert groups[0]["text"] == "This is Decoder."  # sentence end forces a break
    for a, b in zip(groups, groups[1:]):
        assert a["end"] <= b["start"]


def test_caption_breaks_on_pauses():
    style = load_theme("default")["captions"]
    w = words_from("one two")
    w[1]["start"], w[1]["end"] = 5.0, 5.3
    assert len(captions.group_words(w, style)) == 2


def test_srt_and_ass_output():
    theme = load_theme("default")
    groups = captions.group_words(words_from("Growth fell {fast} toward zero."), theme["captions"])
    srt = captions.to_srt(groups)
    assert srt.startswith("1\n00:00:00,000 --> ")
    ass = captions.to_ass(groups, theme["captions"], theme["canvas"], theme["layout"])
    assert "PlayResY: 1920" in ass and "{fast}" not in ass and "(fast)" in ass
    assert "\\c&H3FD2FF&" in ass  # active colour #ffd23f as &HBBGGRR&


def test_caption_seam_position():
    theme = load_theme("default")
    style = {**theme["captions"], "position": "seam"}
    assert captions.caption_anchor(style, theme["layout"]) == (8, theme["layout"]["talent"]["y"] + 24)


# ---------------------------------------------------------------- cues

def test_timecode():
    assert cues.timecode(61.5, 30) == "00:01:01:15"


def test_align_maps_unchanged_runs():
    m = cues.align("a b c d e".split(), "a b x d e".split())
    assert m == {0: 0, 1: 1, 3: 3, 4: 4}


@pytest.fixture
def project():
    db.init()
    data, theme = parse_csv(SAMPLE), load_theme("default")
    cfg = get("line_draw").default_config(data, theme)
    pid = db.create_project("t", "line_draw", "default", cfg, data, "x.csv")
    yield pid
    db.delete_project(pid)


def test_reattach_ok_moved_orphaned(project):
    old = db.add_transcript(project, words_from(
        "Canada grew fast. Then Ottawa cut targets to 1.2 million and growth fell."), "import", "en")
    idx = {w["text"]: w["i"] for w in old["words"]}
    c_ok = db.add_cue(project, old["id"], idx["Ottawa"], "ottawa", "hold", {})
    c_moved = db.add_cue(project, old["id"], idx["1.2"], "1.2", "hold", {})
    c_gone = db.add_cue(project, old["id"], idx["fast."], "fast", "hold", {})
    # Re-take: an extra intro sentence shifts every index; "fast" is gone; Whisper
    # writes the number differently this time ("1.20"), so that cue is fuzzily "moved".
    new = db.add_transcript(project, words_from(
        "Hi, it's Decoder. Canada grew quickly. Then Ottawa cut targets to 1.20 million and growth fell."),
        "import", "en")
    counts = cues.reattach(project, new)
    assert counts == {"ok": 1, "moved": 1, "orphaned": 1}
    nw = [w["text"] for w in new["words"]]
    assert nw[db.get_cue(c_ok)["word_index"]] == "Ottawa" and db.get_cue(c_ok)["status"] == "ok"
    assert nw[db.get_cue(c_moved)["word_index"]] == "1.20" and db.get_cue(c_moved)["status"] == "moved"
    assert db.get_cue(c_gone)["status"] == "orphaned"
    timed = cues.timed_cues(project, new)
    assert [c["id"] for c in timed] == [c_ok, c_moved]  # orphans are left out of the render
    assert timed[0]["t"] == new["words"][nw.index("Ottawa")]["start"]


def test_load_wav_for_whisper(tmp_path):
    """Whisper gets samples straight from our WAV, not via PyAV."""
    import subprocess

    from decoder import config
    from decoder.media import extract_wav
    from decoder.transcribe import load_wav

    src, wav = tmp_path / "tone.mp4", tmp_path / "tone.wav"
    subprocess.run([config.FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=2",
                    "-c:a", "aac", str(src)], check=True)
    extract_wav(src, wav)
    a = load_wav(wav)
    assert a.dtype.name == "float32" and abs(len(a) - 32000) < 1600 and 0 < abs(a).max() <= 1.0


# ---------------------------------------------------------------- transcript edits

from decoder import transcript_edit as te  # noqa: E402


def test_join_continuations_repairs_split_numbers():
    words = [{"i": 0, "text": "about", "start": 0, "end": .3, "glued": False},
             {"i": 1, "text": "1", "start": .35, "end": .5, "glued": False},
             {"i": 2, "text": ".2", "start": .5, "end": .7, "glued": True},
             {"i": 3, "text": "million", "start": .75, "end": 1.1, "glued": False},
             {"i": 4, "text": "%", "start": 1.1, "end": 1.2, "glued": True}]
    out = te.join_continuations(words)
    assert [w["text"] for w in out] == ["about", "1.2", "million%"]
    assert out[1]["start"] == .35 and out[1]["end"] == .7 and "glued" not in out[1]
    assert [w["i"] for w in out] == [0, 1, 2]


def test_join_continuations_leaves_separate_words_alone():
    words = [{"i": 0, "text": "Canada", "start": 0, "end": .3, "glued": False},
             {"i": 1, "text": "Then", "start": .35, "end": .5, "glued": True}]  # letters: never merged
    assert [w["text"] for w in te.join_continuations(words)] == ["Canada", "Then"]


def test_transcript_edits_keep_cues_on_their_words(project):
    t = db.add_transcript(project, words_from("adding 1 .2 million people a year"), "import", "en")
    on_million = db.add_cue(project, t["id"], 3, "million", "hold", {})
    on_people = db.add_cue(project, t["id"], 4, "people", "hold", {})
    te.apply(project, "merge", 1)                      # "1" + ".2"
    w = db.latest_transcript(project)["words"]
    assert [x["text"] for x in w][:3] == ["adding", "1.2", "million"]
    assert db.get_cue(on_million)["word_index"] == 2
    te.apply(project, "text", 0, "now adding")         # split one word into two
    w = db.latest_transcript(project)["words"]
    assert [x["text"] for x in w][:3] == ["now", "adding", "1.2"]
    assert db.get_cue(on_million)["word_index"] == 3 and w[3]["text"] == "million"
    te.apply(project, "delete", 3)                     # cue on a deleted word moves to the next one
    w = db.latest_transcript(project)["words"]
    assert w[db.get_cue(on_million)["word_index"]]["text"] == "people"
    assert w[db.get_cue(on_people)["word_index"]]["text"] == "people"
    assert db.get_cue(on_million)["anchor_word"] == "people"


# ---------------------------------------------------------------- overlay + start-from cues

def test_overlay_and_start_from_cues_validate():
    data, theme = parse_csv(SAMPLE), load_theme("default")
    T = get("line_split")
    cfg = T.default_config(data, theme)
    clean, errs = T.validate_cue("show_text", {"slot": "2", "text": "Target for non-permanent\r\npopulation share",
                                               "size": "64", "color": "#eb6834", "effect": "fade"}, cfg, data)
    assert not errs and clean["text"] == "Target for non-permanent\npopulation share" and clean["size"] == 64
    _, errs = T.validate_cue("show_text", {"text": "  ", "color": "red"}, cfg, data)
    assert len(errs) == 2  # empty text, bad colour
    clean, errs = T.validate_cue("dim_chart", {}, cfg, data)
    assert not errs and "amount" not in clean  # theme default used at render time
    clean, errs = T.validate_cue("draw_line", {"series": "revised_estimate", "from": "2025-Q2"}, cfg, data)
    assert not errs and clean["from"] == "2025-Q2"
    clean, _ = T.validate_cue("draw_line", {"series": "revised_estimate", "from": ""}, cfg, data)
    assert "from" not in clean
