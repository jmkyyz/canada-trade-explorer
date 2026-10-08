"""Cue timing, re-attachment after a re-take, and the cue sheet export.

A cue is anchored to a *word* (transcript id + word index), never to a raw
time, so animation timing follows the narration. Its time is
    words[word_index].start + offset

Re-attach: when a new take is transcribed, the old transcript's words are
aligned to the new transcript's words (difflib on normalised words, which
finds the longest common runs, i.e. the parts of the script that didn't
change). A cue whose anchor word survives moves to it ("ok"); otherwise we
look for the same word near where the alignment says it should be, scoring
neighbouring words for context ("moved"); failing that the cue is kept but
flagged "orphaned" for the user to re-click.
"""
import csv
import difflib
import io
import json

from . import db
from .templates import get as get_template
from .transcribe import norm


def cue_time(cue: dict, words: list) -> float | None:
    i = cue["word_index"]
    if 0 <= i < len(words):
        return round(words[i]["start"] + (cue.get("offset") or 0), 3)
    return None


def timed_cues(project_id: int, transcript: dict | None) -> list[dict]:
    """Cues for the current transcript with resolved times, in time order."""
    if not transcript:
        return []
    words = transcript["words"]
    out = []
    for c in db.list_cues(project_id):
        if c["transcript_id"] != transcript["id"] or c["status"] == "orphaned":
            continue
        t = cue_time(c, words)
        if t is None:
            continue
        out.append({**c, "t": max(0.0, t), "word": words[c["word_index"]]["text"]})
    out.sort(key=lambda c: (c["t"], c["id"]))
    return out


def _context_score(old_words, oi, new_words, ni, span=3):
    score = 0
    for d in range(1, span + 1):
        for sign in (-1, 1):
            a, b = oi + sign * d, ni + sign * d
            if 0 <= a < len(old_words) and 0 <= b < len(new_words) and old_words[a] == new_words[b]:
                score += 1.0 / d
    return score


def align(old: list[str], new: list[str]) -> dict[int, int]:
    """Map old word index -> new word index for words in matching runs."""
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    mapping = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            mapping[a + k] = b + k
    return mapping


def _expected_position(oi, mapping, n_new):
    """Where an unmatched old word 'should' be in the new transcript."""
    before = [k for k in mapping if k < oi]
    after = [k for k in mapping if k > oi]
    if before:
        k = max(before)
        return min(n_new - 1, mapping[k] + (oi - k))
    if after:
        k = min(after)
        return max(0, mapping[k] - (k - oi))
    return 0


def reattach(project_id: int, new_transcript: dict) -> dict:
    """Move every cue onto new_transcript. Returns counts by status."""
    new_words = [norm(w["text"]) for w in new_transcript["words"]]
    cache = {}
    counts = {"ok": 0, "moved": 0, "orphaned": 0}
    for c in db.list_cues(project_id):
        if c["transcript_id"] == new_transcript["id"]:
            continue
        if c["transcript_id"] not in cache:
            old_t = db.get_transcript(c["transcript_id"])
            old_words = [norm(w["text"]) for w in old_t["words"]] if old_t else []
            cache[c["transcript_id"]] = (old_words, align(old_words, new_words))
        old_words, mapping = cache[c["transcript_id"]]
        oi = c["word_index"]
        target = norm(c["anchor_word"])
        status, ni = "orphaned", oi
        if oi in mapping and new_words[mapping[oi]] == target:
            status, ni = "ok", mapping[oi]
        elif new_words:
            guess = _expected_position(oi, mapping, len(new_words))
            window = range(max(0, guess - 25), min(len(new_words), guess + 26))
            cands = [j for j in window if new_words[j] == target]
            if not cands:  # word was rephrased; accept a near-spelling (e.g. "1.2" vs "1.20")
                cands = [j for j in window
                         if difflib.SequenceMatcher(a=target, b=new_words[j]).ratio() >= 0.8]
            if cands:
                ni = max(cands, key=lambda j: (_context_score(old_words, oi, new_words, j),
                                               -abs(j - guess)))
                status = "moved"
        db.update_cue(c["id"], transcript_id=new_transcript["id"],
                      word_index=min(ni, max(0, len(new_words) - 1)), status=status)
        counts[status] += 1
    return counts


def cue_sheet(project: dict, transcript: dict, fps: int) -> list[dict]:
    tpl = get_template(project["template"])
    rows = []
    for c in timed_cues(project["id"], transcript):
        t = c["t"]
        rows.append({
            "cue_id": c["id"],
            "time_seconds": round(t, 3),
            "timecode": timecode(t, fps),
            "frame": int(round(t * fps)),
            "word_index": c["word_index"],
            "word": c["word"],
            "action": c["action"],
            "description": tpl.describe_cue(c["action"], c["params"], project["config"]),
            "params": c["params"],
        })
    return rows


def timecode(t: float, fps: int) -> str:
    frames = int(round(t * fps))
    h, rem = divmod(frames, 3600 * fps)
    m, rem = divmod(rem, 60 * fps)
    s, f = divmod(rem, fps)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def cue_sheet_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    fields = ["cue_id", "timecode", "time_seconds", "frame", "word", "word_index", "action",
              "description", "params"]
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow({**{k: r[k] for k in fields if k != "params"}, "params": json.dumps(r["params"])})
    return buf.getvalue()
