"""Make a synthetic portrait test take: placeholder presenter + TTS narration
with exact word timings (so the pipeline can be tested without a real
recording, or without a Whisper model).

    cd scripts/testvideo && npm install && cd ../..
    python scripts/make_test_video.py scripts/testvideo/take1.txt test_take1 [--label "TAKE 1"]

Writes <out>.mov (1080x1920, 29.97 fps like a phone) and <out>.words.json
(importable on the Video tab under "Import word timings").
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

PRESENTER_HTML = """<!doctype html><html><body style="margin:0">
<div style="width:1080px;height:1920px;position:relative;overflow:hidden;
  background:linear-gradient(160deg,#3a4a5c 0%,#1d2530 70%);font-family:Inter,Arial,sans-serif">
  <div style="position:absolute;left:340px;top:560px;width:400px;height:500px;border-radius:50%;
    background:#c99a7a"></div>
  <div style="position:absolute;left:150px;top:1080px;width:780px;height:1000px;border-radius:390px 390px 0 0;
    background:#2f5f8f"></div>
  <div style="position:absolute;left:0;right:0;top:120px;text-align:center;color:#fff;font-size:64px;
    font-weight:700;letter-spacing:4px">TEST PRESENTER · __LABEL__</div>
  <div style="position:absolute;left:0;right:0;top:1500px;text-align:center;color:#ffffffaa;font-size:44px">
    synthetic voice · placeholder take</div>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("out", help="output path without extension")
    ap.add_argument("--label", default="TAKE")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    wav, words = out.with_suffix(".wav"), out.with_suffix(".words.json")
    png, html = out.with_suffix(".presenter.png"), out.with_suffix(".presenter.html")

    subprocess.run(["node", str(HERE / "testvideo" / "tts_words.mjs"), str(Path(a.script).resolve()),
                    str(wav), str(words)], check=True, cwd=HERE / "testvideo")

    from playwright.sync_api import sync_playwright
    html.write_text(PRESENTER_HTML.replace("__LABEL__", a.label))
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1080, "height": 1920})
        pg.goto(html.as_uri())
        pg.screenshot(path=str(png))
        b.close()

    # Burn the running time into the picture so preview/render sync is visible.
    vf = ("drawtext=font=Inter:fontsize=72:fontcolor=white:box=1:boxcolor=black@0.6:"
          "x=(w-tw)/2:y=1700:text='%{pts\\:hms}'")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", "30000/1001",
                    "-i", str(png), "-i", str(wav), "-vf", vf, "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                    "-c:a", "aac", "-ar", "44100", str(out.with_suffix(".mov"))], check=True)
    for f in (wav, png, html):
        f.unlink()
    print(out.with_suffix(".mov"), words)


if __name__ == "__main__":
    sys.exit(main())
