"""Render a project.

  1. capture   chart page in headless Chromium (Playwright), one PNG per frame
               at canvas fps. Frames whose state signature hasn't changed are
               hard-linked to the previous PNG instead of re-screenshotted.
  2. overlay   chart-only deliverables with alpha: ProRes 4444 .mov and a
               full-canvas PNG sequence
  3. captions  SRT + styled ASS from the transcript
  4. compose   talent video cropped into the talent region + chart frames +
               burned-in captions -> H.264 1080x1920 MP4
  5. extras    cue sheet (CSV + JSON), transcript JSON, spec JSON

Usable from the web app (background job) or the CLI:
    python -m decoder.render <project_id>
"""
import json
import math
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import config, db, media, segmentation
from .captions import group_words, to_ass, to_srt
from .cues import cue_sheet, cue_sheet_csv
from .spec import build_spec, chart_page, static_file_base
from .theme import fonts_dir


class RenderError(RuntimeError):
    pass


def _link(src: Path, dst: Path):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def capture_frames(page_path: Path, frames_dir: Path, n_frames: int, fps: int,
                   width: int, height: int, workers: int, progress_cb=None) -> dict:
    """Screenshot chart frames 0..n_frames-1 into frames_dir/f_00000.png ..."""
    from playwright.sync_api import sync_playwright

    frames_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(workers, n_frames // 60 or 1))
    bounds = [round(i * n_frames / workers) for i in range(workers + 1)]
    done = [0] * workers
    stats = {"screenshots": 0, "reused": 0}
    lock = threading.Lock()
    errors = []

    def work(w):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": width, "height": height},
                                        device_scale_factor=1)
                page.goto(page_path.resolve().as_uri())
                page.wait_for_function("window.__ready === true", timeout=30000)
                prev_sig, prev_file = None, None
                for i in range(bounds[w], bounds[w + 1]):
                    sig = page.evaluate("t => window.__seek(t)", i / fps)
                    out = frames_dir / f"f_{i:05d}.png"
                    if sig == prev_sig and prev_file:
                        _link(prev_file, out)
                        with lock:
                            stats["reused"] += 1
                    else:
                        page.screenshot(path=str(out), omit_background=True,
                                        clip={"x": 0, "y": 0, "width": width, "height": height})
                        with lock:
                            stats["screenshots"] += 1
                    prev_sig, prev_file = sig, out
                    done[w] = i - bounds[w] + 1
                    if progress_cb and i % 15 == 0:
                        progress_cb(sum(done) / n_frames)
                browser.close()
        except Exception as e:  # surface in the main thread
            errors.append(e)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise RenderError(f"Frame capture failed: {errors[0]}")
    return stats


def _talent_filter(theme: dict, layout: dict) -> str:
    """Crop the full-frame talent master into the talent region."""
    canvas, region = theme["canvas"], theme["layout"]["talent"]
    zoom = float(layout.get("talent_zoom", 1.0))
    center = float(layout.get("talent_crop_center", region.get("default_crop_center", 0.42)))
    sw = int(round(canvas["width"] * zoom / 2) * 2)
    sh = int(round(canvas["height"] * zoom / 2) * 2)
    rw, rh = region["width"], region["height"]
    x = max(0, (sw - rw) // 2)
    y = int(min(max(0, center * sh - rh / 2), sh - rh))
    return f"scale={sw}:{sh},crop={rw}:{rh}:{x}:{y},setsar=1"


def compose(master: Path, frames_pattern: str, ass_path: Path, out: Path, theme: dict,
            layout: dict, duration: float, theme_name: str, progress_cb=None):
    if layout.get("mode", "stacked") == "presenter":
        raise RenderError(segmentation.PHASE2_MESSAGE)
    canvas, L = theme["canvas"], theme["layout"]
    fps = canvas["fps"]
    fdir = fonts_dir(theme_name)
    ass_opts = f"ass=filename='{ass_path.name}'" + (f":fontsdir='{fdir}'" if fdir else "")
    graph = (
        f"color=c={canvas['background']}:s={canvas['width']}x{canvas['height']}:r={fps}:d={duration:.3f}[bg];"
        f"[0:v]{_talent_filter(theme, layout)}[talent];"
        f"[bg][talent]overlay={L['talent']['x']}:{L['talent']['y']}:shortest=1[b1];"
        f"[1:v]format=rgba[chart];"
        f"[b1][chart]overlay={L['chart']['x']}:{L['chart']['y']}:format=auto:eof_action=repeat[b2];"
        f"[b2]{ass_opts},format=yuv420p[out]"
    )
    cmd = [config.FFMPEG, "-y", "-i", str(master), "-framerate", str(fps), "-i", frames_pattern,
           "-filter_complex", graph, "-map", "[out]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
           "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.3f}",
           "-movflags", "+faststart", str(out)]
    # Run from the output dir: the ass= path stays relative, so no filter escaping.
    media.run(cmd, progress_cb, duration, cwd=out.parent)


def render_project(pid: int, progress=None, keep_frames=False) -> dict:
    """progress(stage, fraction, message) is optional."""
    say = progress or (lambda *a: None)
    project = db.get_project(pid)
    if not project:
        raise RenderError("No such project")
    if not project["video"]:
        raise RenderError("Upload a video first")
    transcript = db.latest_transcript(pid)
    if not transcript:
        raise RenderError("Transcribe the video first")

    spec = build_spec(project, transcript)
    theme = spec["theme"]
    fps = theme["canvas"]["fps"]
    pdir = config.project_dir(pid)
    master = pdir / project["video"]["master"]
    duration = media.probe(master)["duration"]
    n_frames = int(math.floor(duration * fps + 1e-6))

    out_dir = pdir / "renders" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_work"
    work.mkdir()

    # 1. chart frames
    page = work / "chart.html"
    page.write_text(chart_page(spec, static_file_base(), project["theme"]))
    cw, ch = spec["width"], spec["height"]
    t0 = time.time()
    say("capture", 0, f"Capturing {n_frames} chart frames")
    stats = capture_frames(page, work / "frames", n_frames, fps, cw, ch, config.RENDER_WORKERS,
                           lambda f: say("capture", f, f"Capturing chart frames ({f:.0%})"))
    capture_secs = time.time() - t0
    frames = str((work / "frames" / "f_%05d.png").resolve())
    canvas, region = theme["canvas"], theme["layout"]["chart"]
    pad = (f"format=rgba,pad={canvas['width']}:{canvas['height']}:{region['x']}:{region['y']}"
           f":color=black@0")

    # 2. overlay deliverables
    say("overlay", 0, "Encoding ProRes 4444 overlay")
    overlay_mov = out_dir / "chart_overlay_prores4444.mov"
    media.run([config.FFMPEG, "-y", "-framerate", str(fps), "-i", frames, "-vf", pad,
               "-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le",
               "-alpha_bits", "16", "-vendor", "apl0", str(overlay_mov)],
              lambda f: say("overlay", f * 0.7, "Encoding ProRes 4444 overlay"), duration)
    png_dir = out_dir / "chart_overlay_png"
    png_dir.mkdir()
    say("overlay", 0.7, "Writing PNG sequence")
    media.run([config.FFMPEG, "-y", "-framerate", str(fps), "-i", frames, "-vf", pad,
               "-start_number", "0", str(png_dir / "chart_%05d.png")])  # frame 0 = 0s, as in the cue sheet

    # 3. captions
    style = theme["captions"]
    groups = group_words(transcript["words"], style)
    (out_dir / "captions.srt").write_text(to_srt(groups))
    ass_path = out_dir / "captions.ass"
    ass_path.write_text(to_ass(groups, style, theme["canvas"], theme["layout"]))

    # 4. final composite
    say("compose", 0, "Compositing final MP4")
    final = out_dir / "decoder_final_1080x1920.mp4"
    compose(master, frames, ass_path, final, theme, project["layout"], duration,
            project["theme"], lambda f: say("compose", f, f"Compositing final MP4 ({f:.0%})"))

    # 5. extras for the video team
    sheet = cue_sheet(project, transcript, fps)
    (out_dir / "cue_sheet.json").write_text(json.dumps(
        {"project": project["name"], "fps": fps, "duration": duration, "cues": sheet}, indent=2))
    (out_dir / "cue_sheet.csv").write_text(cue_sheet_csv(sheet))
    (out_dir / "transcript.json").write_text(json.dumps(transcript["words"], indent=1))
    (out_dir / "chart_spec.json").write_text(json.dumps(spec, indent=1))

    if not keep_frames:
        shutil.rmtree(work)

    rel = lambda p: str(p.relative_to(pdir))
    result = {
        "dir": rel(out_dir),
        "final": rel(final),
        "overlay_mov": rel(overlay_mov),
        "overlay_png_dir": rel(png_dir),
        "srt": rel(out_dir / "captions.srt"),
        "ass": rel(ass_path),
        "cue_sheet_csv": rel(out_dir / "cue_sheet.csv"),
        "cue_sheet_json": rel(out_dir / "cue_sheet.json"),
        "transcript_json": rel(out_dir / "transcript.json"),
        "frames": n_frames, "duration": duration,
        "capture": {**stats, "seconds": round(capture_secs, 1)},
    }
    (out_dir / "render.json").write_text(json.dumps(result, indent=2))
    say("done", 1, "Render complete")
    return result


if __name__ == "__main__":
    db.init()
    res = render_project(int(sys.argv[1]),
                         lambda s, f, m: print(f"\r[{s}] {m}", end="", flush=True),
                         keep_frames="--keep-frames" in sys.argv)
    print("\n" + json.dumps(res, indent=2))
