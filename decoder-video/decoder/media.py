"""FFmpeg/ffprobe helpers for the talent (talking-head) video."""
import json
import subprocess
import tempfile
from pathlib import Path

from . import config


class MediaError(RuntimeError):
    pass


def run(cmd: list, progress_cb=None, duration=None, cwd=None):
    """Run ffmpeg, optionally reporting progress (0..1) from -progress output."""
    if progress_cb and duration:
        cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
        # stderr goes to a file: a full stderr pipe would stall ffmpeg.
        with tempfile.TemporaryFile("w+") as errf:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True, cwd=cwd)
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        progress_cb(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
                    except ValueError:
                        pass
            if proc.wait() != 0:
                errf.seek(0)
                raise MediaError(errf.read()[-3000:])
        return
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if res.returncode != 0:
        raise MediaError(res.stderr[-3000:])
    return res


def probe(path: Path) -> dict:
    res = subprocess.run(
        [config.FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)], capture_output=True, text=True)
    if res.returncode != 0:
        raise MediaError(f"ffprobe could not read {path.name}: {res.stderr.strip()}")
    info = json.loads(res.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        raise MediaError("No video stream found")
    w, h = int(v["width"]), int(v["height"])
    # Phones store portrait video as landscape + a rotation flag.
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int(v.get("tags", {}).get("rotate", 0) or 0)
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {
        "duration": float(info["format"].get("duration") or v.get("duration") or 0),
        "width": w, "height": h, "rotation": rot,
        "codec": v.get("codec_name"), "has_audio": a is not None,
        "fps": v.get("avg_frame_rate"),
    }


def normalize(src: Path, dst: Path, fps: int, width: int, height: int, progress_cb=None) -> dict:
    """Make one clean master: H.264, CFR, exact canvas size, AAC, timestamps from 0.

    Phone footage is often HEVC, variable frame rate, rotated, and starts at a
    non-zero timestamp. Normalising once means the browser preview, Whisper
    timestamps and the final composite all share the same clock.
    """
    info = probe(src)
    if not info["has_audio"]:
        raise MediaError("The video has no audio track, so there is nothing to transcribe")
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},setsar=1,fps={fps}")
    cmd = [config.FFMPEG, "-y", "-i", str(src), "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-af", "aresample=async=1:first_pts=0",
           "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(dst)]
    run(cmd, progress_cb, info["duration"])
    out = probe(dst)
    out["source"] = info
    return out


def extract_wav(src: Path, dst: Path):
    run([config.FFMPEG, "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dst)])
