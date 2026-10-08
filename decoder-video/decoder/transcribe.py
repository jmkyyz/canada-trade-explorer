"""Speech to word-level timestamps with faster-whisper.

Word shape everywhere in the app:
    {"i": 0, "text": "Canada's", "start": 1.24, "end": 1.71, "prob": 0.98}
``text`` keeps punctuation (captions use it); matching uses ``norm(text)``.
"""
import re
import threading

from . import config

_model = None
_model_lock = threading.Lock()


def norm(word: str) -> str:
    """Lower-case, strip punctuation; keeps digits and inner apostrophes."""
    w = word.lower().replace("’", "'")
    w = re.sub(r"[^\w'.%-]", "", w)
    return w.strip(".'-")


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel  # heavy import; only when needed
            _model = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE,
                                  compute_type=config.WHISPER_COMPUTE)
        return _model


def transcribe(wav_path, prompt: str = "", progress_cb=None, duration: float = None) -> dict:
    """Return {"words": [...], "language": "en", "source": "faster-whisper:<model>"}.

    ``prompt`` biases spelling toward the chart's vocabulary (series names,
    "Statistics Canada" ...), which helps with proper nouns and numbers.
    """
    model = get_model()
    segments, info = model.transcribe(
        str(wav_path),
        language="en" if config.WHISPER_MODEL.endswith(".en") else None,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        initial_prompt=prompt or None,
        condition_on_previous_text=False,
    )
    words = []
    for seg in segments:  # generator: transcription happens while iterating
        for w in seg.words or []:
            text = w.word.strip()
            if not text:
                continue
            words.append({"i": len(words), "text": text, "start": round(w.start, 3),
                          "end": round(w.end, 3), "prob": round(w.probability, 3)})
        if progress_cb and duration:
            progress_cb(min(0.99, seg.end / duration))
    return {"words": words, "language": info.language,
            "source": f"faster-whisper:{config.WHISPER_MODEL}"}


def clean_imported(words: list) -> list:
    """Validate words imported from JSON (another tool, or a test fixture)."""
    out = []
    for w in words:
        text = str(w.get("text", w.get("word", ""))).strip()
        if not text:
            continue
        start, end = float(w["start"]), float(w["end"])
        if end < start:
            raise ValueError(f"Word '{text}' ends before it starts")
        out.append({"i": len(out), "text": text, "start": round(start, 3), "end": round(end, 3),
                    "prob": float(w.get("prob", w.get("probability", 1.0)))})
    out.sort(key=lambda w: w["start"])
    for i, w in enumerate(out):
        w["i"] = i
    return out
