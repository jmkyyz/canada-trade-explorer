"""Build the chart spec consumed by static/js/engine.js, and the standalone
chart page used for frame capture (and served for the preview iframe)."""
import html
import json

from . import config, db
from .cues import timed_cues
from .theme import load_theme, theme_css


def build_spec(project: dict, transcript: dict | None = None) -> dict:
    transcript = transcript if transcript is not None else db.latest_transcript(project["id"])
    theme = load_theme(project["theme"])
    cues = [{"id": c["id"], "t": c["t"], "action": c["action"], "params": c["params"]}
            for c in timed_cues(project["id"], transcript)]
    return {
        "template": project["template"],
        "config": project["config"],
        "data": project["data"],
        "theme": theme,
        "cues": cues,
        "width": theme["layout"]["chart"]["width"],
        "height": theme["layout"]["chart"]["height"],
    }


def chart_page(spec: dict, static_base: str, theme_name: str) -> str:
    """A page that holds only the chart, transparent outside the chart panel.

    Exposes window.__seek(t) -> state signature, and sets window.__ready once
    fonts are loaded and the chart is built (text wrapping measures fonts).
    """
    css = theme_css(theme_name)
    spec_json = json.dumps(spec).replace("</", "<\\/")
    w, h = spec["width"], spec["height"]
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(spec['config'].get('title') or 'chart')}</title>
<style>
html, body {{ margin: 0; padding: 0; background: transparent; overflow: hidden; }}
#chart {{ width: {w}px; height: {h}px; }}
{css}
</style>
<script src="{static_base}/vendor/d3.v7.9.0.min.js"></script>
<script src="{static_base}/js/engine.js"></script>
{''.join(f'<script src="{static_base}/js/templates/{n}.js"></script>' for n in template_scripts(spec['template']))}
</head><body><div id="chart"></div>
<script>
const SPEC = {spec_json};
let chart = null;
// __load lets the editor preview rebuild the chart after an edit without a reload.
window.__load = spec => {{ chart = Decoder.createChart(document.getElementById("chart"), spec); }};
window.__seek = t => chart.seek(t);
document.fonts.ready.then(() => {{
  window.__load(SPEC);
  window.__seek(0);
  window.__ready = true;
}});
</script></body></html>"""


def template_scripts(name: str) -> list[str]:
    """Line templates share the LineBase in line_draw.js."""
    deps = {"line_split": ["line_draw", "line_split"]}
    return deps.get(name, [name])


def static_file_base() -> str:
    return config.STATIC_DIR.resolve().as_uri()
