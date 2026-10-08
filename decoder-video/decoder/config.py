"""Paths and settings. Everything can be overridden with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("DECODER_DATA_DIR", ROOT / "projects")).resolve()
DB_PATH = Path(os.environ.get("DECODER_DB", DATA_DIR / "decoder.sqlite3"))
THEMES_DIR = ROOT / "themes"
STATIC_DIR = ROOT / "static"
DEFAULT_THEME = os.environ.get("DECODER_THEME", "default")

# faster-whisper. small.en is a good speed/accuracy trade-off on an M-series
# Mac CPU; medium.en is more accurate on names and numbers but ~3x slower.
WHISPER_MODEL = os.environ.get("DECODER_WHISPER_MODEL", "small.en")
WHISPER_DEVICE = os.environ.get("DECODER_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("DECODER_WHISPER_COMPUTE", "int8")

FFMPEG = os.environ.get("DECODER_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("DECODER_FFPROBE", "ffprobe")

# Parallel Playwright pages used to capture chart frames.
RENDER_WORKERS = int(os.environ.get("DECODER_RENDER_WORKERS", "3"))

MAX_VIDEO_SECONDS = 120


def project_dir(project_id: int) -> Path:
    d = DATA_DIR / f"p{project_id:04d}"
    d.mkdir(parents=True, exist_ok=True)
    return d
