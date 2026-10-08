"""End-to-end run through the HTTP API (Flask test client, real jobs):
create project -> upload take -> import word timings (or Whisper) ->
tag cues on words -> render -> (optionally) upload a re-take and check re-attach.

    python scripts/make_test_video.py scripts/testvideo/take1.txt media/take1 --label "TAKE 1"
    python scripts/e2e_demo.py line_draw media/take1 [--retake media/take2] [--whisper]

Without --whisper the .words.json written next to the take is imported
instead of running faster-whisper.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402

# Cue plans: (word to find, occurrence, action, params)
PLANS = {
    "line_draw": [
        ("Decoder.", 1, "show_chart", {"duration": 0.6}),
        ("population", 1, "draw_line", {"series": "previous_estimate", "to": "2021-Q1", "duration": 3}),
        ("closed,", 1, "show_annotation", {"annotation": 1}),
        ("surged.", 1, "draw_line", {"series": "previous_estimate", "to": "2023-Q4", "duration": 2.5}),
        ("million", 1, "highlight_point", {"series": "previous_estimate", "x": "2023-Q4", "label": True, "pulses": 2}),
        ("record.", 1, "hide_annotation", {"annotation": 1}),
        ("Ottawa", 1, "draw_line", {"series": "previous_estimate", "to": "end", "duration": 2.5}),
        ("targets,", 1, "show_annotation", {"annotation": 3}),
        ("next?", 1, "hold", {"note": "Let it land"}),
    ],
    "line_split": [
        ("Decoder.", 1, "show_chart", {"duration": 0.6}),
        ("population", 1, "draw_line", {"series": "previous_estimate", "to": "2023-Q4", "duration": 4}),
        ("record.", 1, "highlight_point", {"series": "previous_estimate", "x": "2023-Q4", "label": True, "pulses": 2}),
        ("Ottawa", 1, "draw_line", {"series": "previous_estimate", "to": "end", "duration": 2.5}),
        ("targets,", 1, "clear_highlights", {}),
        ("zero.", 1, "split_line", {"series": "revised_estimate", "duration": 2.5}),
        ("happens", 1, "shade_gap", {"duration": 0.8}),
        ("next?", 1, "label_gap", {"x": "end"}),
    ],
}


def wait(client, jid, label):
    last = None
    while True:
        j = client.get(f"/api/jobs/{jid}").get_json()
        line = f"  {label}: {j['status']} {j.get('message') or ''}"
        if line != last and not line.endswith("%)"):
            print(line, flush=True)
            last = line
        if j["status"] in ("done", "error"):
            if j["status"] == "error":
                raise SystemExit(f"{label} failed: {j['message']}")
            return j
        time.sleep(0.5)


def upload(client, pid, take: Path, whisper: bool):
    with open(take.with_suffix(".mov"), "rb") as f:
        r = client.post(f"/api/projects/{pid}/video",
                        data={"video": (f, take.with_suffix(".mov").name),
                              "transcribe": "1" if whisper else "0"},
                        content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    wait(client, r.get_json()["job"], "normalise")
    if whisper:
        time.sleep(0.5)
        p = client.get(f"/api/projects/{pid}").get_json()
        return wait(client, p["jobs"]["transcribe"]["id"], "transcribe")["result"]
    words = json.loads(take.with_suffix(".words.json").read_text())
    r = client.post(f"/api/projects/{pid}/transcript/import", json=words)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("template")
    ap.add_argument("take", help="path to a take without extension (expects .mov and .words.json)")
    ap.add_argument("--retake")
    ap.add_argument("--whisper", action="store_true")
    ap.add_argument("--no-render", action="store_true")
    a = ap.parse_args()
    c = app.test_client()

    r = c.post("/api/projects", data={"template": a.template, "sample": "1"},
               headers={"Accept": "application/json"})
    pid = r.get_json()["id"]
    print(f"project {pid} ({a.template})")
    print(" ", upload(c, pid, Path(a.take), a.whisper).get("message"))

    words = c.get(f"/api/projects/{pid}").get_json()["transcript"]["words"]
    for word, nth, action, params in PLANS[a.template]:
        idx = [w["i"] for w in words if w["text"] == word][nth - 1]
        r = c.post(f"/api/projects/{pid}/cues", json={"word_index": idx, "action": action, "params": params})
        assert r.status_code == 200, (word, r.get_json())
    sheet = c.get(f"/api/projects/{pid}/cuesheet.json").get_json()
    for row in sheet["cues"]:
        print(f"  {row['timecode']}  {row['word']:<12} {row['description']}")

    if a.retake:
        res = upload(c, pid, Path(a.retake), a.whisper)
        print(" ", res.get("message"))
        p = c.get(f"/api/projects/{pid}").get_json()
        for cue in p["cues"]:
            w = p["transcript"]["words"][cue["word_index"]]["text"]
            print(f"  [{cue['status']:<8}] {cue['anchor_word']:<12} -> {w:<12} t={cue['t']}")

    if not a.no_render:
        r = c.post(f"/api/projects/{pid}/render")
        res = wait(c, r.get_json()["job"], "render")["result"]
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
