"""Check this machine has what Decoder video needs.  python scripts/check_setup.py"""
import shutil
import subprocess
import sys

ok = True


def check(name, cond, fix=""):
    global ok
    print(f"  [{'ok' if cond else 'MISSING'}] {name}" + ("" if cond else f"  ->  {fix}"))
    ok &= bool(cond)


print("FFmpeg")
ff = shutil.which("ffmpeg")
check("ffmpeg on PATH", ff, "brew install ffmpeg")
if ff:
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    flt = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    check("libx264 encoder", " libx264 " in enc, "brew reinstall ffmpeg")
    check("prores_ks encoder (ProRes 4444)", " prores_ks " in enc, "brew reinstall ffmpeg")
    check("ass filter (libass, burned-in captions)", " ass " in flt, "brew reinstall ffmpeg")
check("ffprobe on PATH", shutil.which("ffprobe"), "brew install ffmpeg")

print("Python packages")
for mod, pkg in (("flask", "flask"), ("faster_whisper", "faster-whisper"), ("playwright", "playwright")):
    try:
        __import__(mod)
        check(pkg, True)
    except ImportError:
        check(pkg, False, "pip install -r requirements.txt")

print("Headless Chromium (Playwright)")
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        check(f"chromium {b.version}", True)
        b.close()
except Exception as e:  # noqa: BLE001
    check("chromium", False, f"python -m playwright install chromium   ({str(e).splitlines()[0][:80]})")

print("\nAll good." if ok else "\nFix the MISSING items above.")
sys.exit(0 if ok else 1)
