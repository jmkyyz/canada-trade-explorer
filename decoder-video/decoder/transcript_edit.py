"""Edits to a transcript after transcription: fix a word, split one word into
several, merge a word with the next, delete a word.

Each edit returns the new word list plus a map old index -> new index, which
is used to keep every cue on the same spoken word.
"""
import re

from . import db
from .transcribe import norm

# A word that continues the previous one: ".2" after "1", "%" after "5",
# ",000" after "1", "'s" after "Canada". Whisper sometimes splits these.
CONTINUATION = re.compile(r"^[.,%'’\-]")


def _reindex(words):
    for i, w in enumerate(words):
        w["i"] = i
    return words


def set_text(words, i, text):
    """Rename word i. Spaces split it into several words (timing shared out by
    length); empty text deletes it."""
    parts = text.split()
    if not parts:
        return delete(words, i)
    w = words[i]
    if len(parts) == 1:
        new = [dict(x) for x in words]
        new[i]["text"] = parts[0]
        return new, {k: k for k in range(len(words))}
    total = sum(len(p) for p in parts)
    t, span, pieces = w["start"], w["end"] - w["start"], []
    for p in parts:
        d = span * len(p) / total
        pieces.append({"text": p, "start": round(t, 3), "end": round(t + d, 3), "prob": w.get("prob", 1)})
        t += d
    new = [dict(x) for x in words[:i]] + pieces + [dict(x) for x in words[i + 1:]]
    extra = len(parts) - 1
    return _reindex(new), {k: (k if k <= i else k + extra) for k in range(len(words))}


def merge_next(words, i):
    """Join word i with word i+1 (no space if the second continues the first)."""
    if i + 1 >= len(words):
        raise ValueError("No next word to merge with")
    a, b = words[i], words[i + 1]
    sep = "" if CONTINUATION.match(b["text"]) or a["text"].endswith(("-", "$")) else " "
    merged = {**a, "text": a["text"] + sep + b["text"], "end": b["end"],
              "prob": min(a.get("prob", 1), b.get("prob", 1))}
    new = [dict(x) for x in words[:i]] + [merged] + [dict(x) for x in words[i + 2:]]
    return _reindex(new), {k: (k if k <= i else k - 1) for k in range(len(words))}


def delete(words, i):
    if len(words) <= 1:
        raise ValueError("Can't delete the only word")
    new = [dict(x) for j, x in enumerate(words) if j != i]
    # cues on the deleted word move to the word that followed it (or the one before, at the end)
    target = i if i < len(new) else i - 1
    return _reindex(new), {k: (k if k < i else target if k == i else k - 1) for k in range(len(words))}


def apply(project_id: int, op: str, i: int, text: str = "") -> dict:
    t = db.latest_transcript(project_id)
    if not t or not 0 <= i < len(t["words"]):
        raise ValueError("No such word")
    if op == "text":
        words, index_map = set_text(t["words"], i, text)
    elif op == "merge":
        words, index_map = merge_next(t["words"], i)
    elif op == "delete":
        words, index_map = delete(t["words"], i)
    else:
        raise ValueError(f"Unknown edit '{op}'")
    db.update_transcript_words(t["id"], words)
    for c in db.list_cues(project_id):
        if c["transcript_id"] != t["id"]:
            continue
        ni = index_map[c["word_index"]]
        # the anchor follows the corrected text, so a later re-take matches the right word
        db.update_cue(c["id"], word_index=ni, anchor_word=norm(words[ni]["text"]))
    return {"words": len(words)}


def join_continuations(words: list) -> list:
    """Repair Whisper splits like "1" ".2" -> "1.2" (only when the second part
    starts with punctuation and follows straight on)."""
    out = []
    for w in words:
        if out and w.get("glued") and CONTINUATION.match(w["text"]) and w["start"] - out[-1]["end"] < 0.3:
            out[-1] = {**out[-1], "text": out[-1]["text"] + w["text"], "end": w["end"],
                       "prob": min(out[-1].get("prob", 1), w.get("prob", 1))}
        else:
            out.append({k: v for k, v in w.items() if k != "glued"})
    return _reindex(out)
