# Decoder video (prototype)

Turns a Decoder chart into a short vertical (9:16) explainer video: you talk on
camera, and the chart animates in step with what you're saying.

```
CSV + chart config ─┐
                    ├─► click a word, attach an action ─► preview ─► render
phone video ─► Whisper word timestamps ┘                               │
                                                                       ▼
        final MP4 · chart overlay with alpha (ProRes 4444 + PNGs) · SRT · cue sheet
```

Status: **line_draw and line_split work end to end.** Phase 2 (cut yourself out
so the chart sits behind you) is stubbed, not built.

## Setup (Mac)

```bash
brew install ffmpeg            # needs libx264, prores_ks and libass (the check below confirms)
cd decoder-video
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python scripts/check_setup.py  # verifies everything above
python app.py                  # http://127.0.0.1:5055/
```

The first transcription downloads the Whisper model (`small.en`, about 480 MB)
to `~/.cache/huggingface`. To change it, set `DECODER_WHISPER_MODEL`:
`medium.en` is more accurate on names and numbers, and about 3x slower.
If `check_setup.py` says the `ass` filter is missing, your FFmpeg build has no
libass. Install a fuller build, such as `brew install ffmpeg-full`, if your
Homebrew has that formula.

## Workflow

1. **New project.** Upload a CSV and pick a template. The first column is x
   (quarters such as `2020-Q1`, months, dates, years, numbers or categories);
   every other column is a series. To try it, tick "Use the sample".
2. **Chart tab.** Set the title, subtitle (optional), source, axis labels, number
   formats, series names, colours and dashes, numbered annotations (text, x,
   which line, above/below, nudge) and template options. "Replace data CSV"
   swaps in new data and keeps the config. Use it to drop in the real StatCan
   series.
3. **Video tab.** Upload the phone video (portrait, under 2 minutes). It's
   normalised once to H.264, 30 fps, 1080×1920 with timestamps from 0, so the
   preview, the Whisper timings and the render all share one clock. It then
   transcribes automatically.
4. **Transcript & cues.** Click a word to jump the preview there, then attach an
   action. You can use an offset in seconds to nudge an action before or after
   the word. Words with cues are underlined, and low-confidence words are
   orange. Double-click a word to fix its spelling: the captions use your
   spelling and the timing stays the same.
5. **Preview.** Press Play (or space). The left panel is the exact 1080×1920
   layout, scaled down. Under "Talent framing", sliders set the vertical crop and
   zoom of your shot.
6. **Render tab.** Renders and lists the outputs (below).

**Re-recording.** Upload the new take on the Video tab. Each cue is re-attached
to the same word in the new transcript and gets one of three statuses:

| Status | What happened |
|---|---|
| matched | The word was in an unchanged run of the script. |
| moved | Fuzzy match: the same word near the expected spot, or a near-spelling such as `1.2` → `1.20`. |
| need attention | The word wasn't found. The cue is kept and flagged; click **Re-attach**, then the new word. |

## Outputs (per render, in `projects/pNNNN/renders/<timestamp>/`)

| File | What it is |
|---|---|
| `decoder_final_1080x1920.mp4` | H.264 High, yuv420p, 30 fps, AAC, captions burned in |
| `chart_overlay_prores4444.mov` | Chart only, full 1080×1920 canvas with alpha (about 300 MB per 35 s) |
| `chart_overlay_png/chart_00000.png …` | Same overlay as an RGBA PNG sequence. Frame N = N/30 s, matching the cue sheet. |
| `captions.srt` | Caption groups (2–4 words) |
| `captions.ass` | The styled captions that were burned in |
| `cue_sheet.csv` / `.json` | Each cue's timecode, frame, word, action, a plain-English description and its parameters |
| `transcript.json`, `chart_spec.json` | Word timings, and the exact spec the chart was drawn from |

The live cue sheet (with no render needed) is linked above the cue list.

## Project structure

```
decoder-video/
  app.py                      Flask routes + background job wiring
  decoder/
    config.py                 paths/env settings
    db.py                     SQLite: projects, transcripts (versioned), cues, jobs
    csvdata.py                CSV -> {x: {type, values, positions}, columns}
    media.py                  ffprobe / normalise / audio extract
    transcribe.py             faster-whisper (word_timestamps, VAD, vocab prompt)
    cues.py                   cue timing, re-attach after a re-take, cue sheet
    captions.py               word grouping, SRT, ASS
    spec.py                   chart spec + the standalone chart page
    render.py                 Playwright capture -> FFmpeg overlay + composite
    segmentation.py           Phase 2 stub (MediaPipe selfie segmentation)
    jobs.py                   one background worker thread
    theme.py                  theme loading
    templates/                Python half of each chart template (manifest)
      base.py                 Template, Action, Param + shared actions
      line_draw.py, line_split.py
  static/
    js/engine.js              timeline + chart.seek(t); shared chrome (title etc.)
    js/templates/*.js         JS half of each chart template (renderer)
    js/editor.js, css/app.css the editor UI
    vendor/d3.v7.9.0.min.js   vendored so it works offline
  themes/default/theme.json   ALL video styling (swap this folder for Globe branding)
  templates/*.html            editor pages (Jinja)
  sample_data/                placeholder Canada population data + chart config
  scripts/                    check_setup, e2e_demo, make_test_video (offline TTS)
  tests/                      pytest (core logic + engine determinism)
```

## Data model

### The key idea: the chart is a pure function of time

Cues compile into a **timeline of tracks**. A track is a number that tweens
over time, such as `head.<series>` (how far a line is drawn, in x units),
`op.<series>` (opacity), `ann.<id>`, `hl.<series>|<x>` and `split.<series>`.
`chart.seek(t)` draws the exact state at second `t`, and nothing runs on
wall-clock timers or d3 transitions. That gives three things:

- The preview calls `seek(video.currentTime)` on every animation frame, so the
  chart can't drift from the video.
- The renderer calls `seek(frame/30)` and screenshots each frame. Several
  Chromium pages capture chunks of the video in parallel.
- `seek` returns a signature of the state. When it hasn't changed (most of a
  talking-head video is a "hold"), the previous PNG is reused. On the test
  take, 740 of 1,074 frames were reused and capture took 9–12 s.

### Chart config (`projects.config_json`)

```jsonc
{
  "title": "...", "subtitle": "...", "source": "Source: ...",
  "x_label": "", "y_label": "People added over previous 12 months",
  "y_format": ",~s", "value_format": ",.0f",        // d3-format strings
  "y_min": null, "y_max": null, "y_zero": true,
  "series": [{"key": "revised_estimate", "column": "revised_estimate",
              "name": "Revised estimate", "color": "#eb6834", "dash": false}],
  "annotations": [{"id": 2, "text": "Peak: 1.27 million in a year", "x": "2023-Q4",
                   "series": "previous_estimate", "position": "above", "dx": 0, "dy": 0}],
  "options": {"split_from": "2025-Q2", "base_series": "previous_estimate"}   // per template
}
```

### Cue (`cues` table)

```jsonc
{
  "transcript_id": 7, "word_index": 31,   // the anchor: a word in a transcript version
  "anchor_word": "surged",                // normalised text, used to re-attach after a re-take
  "offset": 0.0,                          // seconds relative to the word's start
  "action": "draw_line",
  "params": {"series": "previous_estimate", "to": "2023-Q4", "duration": 2.5, "ease": "cubic-in-out"},
  "status": "ok"                          // ok | moved | orphaned
}
```

The time is computed, never stored: `words[word_index].start + offset`. That's
why the timing follows your delivery when you re-record.

### Actions

| Template | Actions |
|---|---|
| all | `show_chart`, `hide_chart`, `show_annotation`, `hide_annotation`, `hold` (a cue-sheet marker only) |
| line_draw | `draw_line` (to an x or the end, continuing from where the line stopped), `reveal_line`, `hide_line`, `highlight_point` (pulse + marker + value), `clear_highlights` |
| line_split | everything in line_draw, plus `split_line` (*peel*: morph away from the base line; *draw*: draw rightward from the split), `shade_gap`, `label_gap` (bracket + "+214,700") |

Before the first `show_chart` cue the chart frame is hidden. With no
`show_chart` cue, the frame is visible from the first frame.

## Adding a template (bars, scatter zoom, big number…)

A template is two files with the same name:

1. `decoder/templates/<name>.py`: subclass `Template`. Declare `actions` (each
   a list of typed `Param`s; the editor builds its forms from these),
   `option_fields`, `default_config`, and optionally `validate_config`,
   `validate_cue` and `describe_cue`. Register it in
   `decoder/templates/__init__.py`.
2. `static/js/templates/<name>.js`: call
   `Decoder.registerTemplate(name, {setup, compile, render})`.
   - `setup(ctx)` builds the SVG once. `ctx.plotRect` is the area left free by
     the title and source.
   - `compile(cue, ctx)` turns the template's own actions into
     `ctx.tl.tween(...)` and `ctx.tl.set(...)` calls.
   - `render(t, ctx, chartOpacity)` reads the tracks at time `t` and updates
     the SVG.

   Never animate any other way, or frames stop being reproducible. If it needs
   a shared base, list the extra script in `spec.template_scripts`.

Sketches for the planned ones, all built on the same track model:

| Planned template | Tracks |
|---|---|
| `bars_sorted` | `bar.<i>` for each bar's length, `order` (a 0→1 tween between unsorted and sorted positions), `hl.<i>` for highlighted bars |
| `scatter_zoom` | `zoom` (0→1 interpolating the scale domains from full to a box around the point), `label.<id>` |
| `big_number` | `count` tweening 0→value, formatted every frame |

## Theme (for the video team)

Everything visual lives in `themes/default/theme.json`:

- canvas size and fps
- layout regions (chart on top, 1080×1152, and the talent region below)
- fonts, title, subtitle and source styles
- axis, gridlines and line widths
- the series palette, annotations and highlights
- caption font, size, colours, outline, position (`bottom` or `seam`), and
  words/characters per caption
- motion timings

To brand it, copy the folder to `themes/globe/`, edit it, and run with
`DECODER_THEME=globe`; new projects can also pick a theme. Put `@font-face`
rules in `theme.css` and font files in `fonts/`. The fonts folder is also
handed to libass, so the burned-in captions use the same fonts.

The placeholder palette is a validated colourblind-safe pair: blue `#2a78d6`,
orange `#eb6834`. The lines carry direct labels at their tips, so colour is
never the only cue.

## Phase 2: chart behind the presenter (stubbed)

`decoder/segmentation.py` holds the plan:

1. MediaPipe selfie segmentation gives a matte for each frame.
2. Smooth it over time and feather the edges.
3. Write a ProRes 4444 matte.
4. Composite in this order: background, then the chart full-frame, then you,
   then captions.

The hooks are already in place:

- `project.layout.mode` is read by `render.compose()`, and `"presenter"` routes
  to the stub. It shows as a disabled option under "Talent framing".
- Theme layouts can add `layout.presenter` regions without code changes.

## Testing

```bash
python -m pytest -q tests                                   # 22 tests: CSV, captions, cues/re-attach, engine purity
cd scripts/testvideo && npm install && cd ../..             # offline TTS (eSpeak-NG WASM) for synthetic takes
python scripts/make_test_video.py scripts/testvideo/take1.txt media/take1 --label "TAKE 1"
python scripts/make_test_video.py scripts/testvideo/take2.txt media/take2 --label "TAKE 2"
python scripts/e2e_demo.py line_draw media/take1 --retake media/take2   # imports exact word timings
python scripts/e2e_demo.py line_split media/take1 --whisper             # uses faster-whisper instead
```

The synthetic takes have the running time burned into the picture, so you can
eyeball sync in the output.

## Known limits / next steps

- **Placeholder data.** The sample CSV is a plausible shape only. Swap in the
  real Statistics Canada series with "Replace data CSV".
- **Caption overlap.** Captions sit 300 px up from the bottom, clear of
  Reels/TikTok UI, which puts them over the lower part of your shot. Adjust
  "Talent framing" so your face sits above them, or set
  `captions.position: "seam"` to put them just under the chart.
- **Annotation placement.** Annotations go above or below their point, with
  manual nudges; there's no automatic collision avoidance between annotations
  yet.
- **Preview codecs.** The preview needs a browser that plays H.264 (Chrome or
  Safari). The open-source Chromium that Playwright uses can't, but it only
  captures the chart, not your video.
- **One job at a time.** Jobs run one after another on a single worker; that's
  deliberate, since Whisper and capture each use every core. An interrupted
  job is marked failed when the server restarts.
