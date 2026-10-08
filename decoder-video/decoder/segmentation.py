"""Phase 2 (stub): cut the presenter out with MediaPipe selfie segmentation so
the chart can sit *behind* them, weather-presenter style.

Planned pipeline (not built yet):
  1. segment_talent(master.mp4) -> talent_matte.mov
       For each frame, mediapipe.solutions.selfie_segmentation
       (model_selection=0, the 256x256 portrait model) gives a person mask.
       Smooth it over time (EMA on the mask) to stop edge flicker, feather the
       edge a few pixels, and write RGBA frames -> ProRes 4444 with alpha.
  2. render with layout mode "presenter":
       background plate (theme colour or image)
       -> chart frames, full canvas (theme.layout.presenter.chart rect)
       -> talent_matte.mov over the top
       -> captions
     i.e. in compose():  [bg][chart]overlay[b1]; [b1][matte]overlay[b2]; [b2]ass=...

Hooks already in place:
  - project.layout["mode"] is read by render.compose(); "presenter" routes here.
  - theme.json can add layout.presenter.{chart,talent} rects without code changes.

Dependencies to add when this is built: mediapipe, opencv-python-headless.
"""

PHASE2_MESSAGE = ("Presenter mode (chart behind you) is Phase 2 and isn't built yet. "
                  "Switch layout mode back to 'stacked'.")


def available() -> bool:
    try:
        import mediapipe  # noqa: F401
        return True
    except ImportError:
        return False


def segment_talent(master_path, out_path, progress_cb=None):
    raise NotImplementedError(PHASE2_MESSAGE)
