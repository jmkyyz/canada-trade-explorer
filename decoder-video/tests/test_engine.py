"""The chart must be a pure function of time: the preview seeks arbitrarily,
and the renderer captures frames in parallel chunks."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from decoder.csvdata import parse_csv  # noqa: E402
from decoder.spec import chart_page, static_file_base  # noqa: E402
from decoder.templates import get  # noqa: E402
from decoder.theme import load_theme  # noqa: E402

sync_api = pytest.importorskip("playwright.sync_api")


def make_page(tmp_path, template, cues):
    data = parse_csv((ROOT / "sample_data" / "canada_population_change.csv").read_text())
    theme = load_theme("default")
    cfg = get(template).default_config(data, theme)
    sample = json.loads((ROOT / "sample_data" / "canada_population_change.config.json").read_text())
    cfg.update(sample["common"])
    cfg.update(sample[template])
    spec = {"template": template, "config": cfg, "data": data, "theme": theme, "cues": cues,
            "width": 1080, "height": 1152}
    page = tmp_path / f"{template}.html"
    page.write_text(chart_page(spec, static_file_base(), "default"))
    return page


CUES = {
    "line_draw": [
        {"t": 0.5, "action": "show_chart", "params": {"duration": 0.6}},
        {"t": 1.0, "action": "draw_line", "params": {"series": "previous_estimate", "to": "2023-Q4", "duration": 3}},
        {"t": 4.5, "action": "highlight_point", "params": {"series": "previous_estimate", "x": "2023-Q4", "pulses": 2}},
        {"t": 5.0, "action": "show_annotation", "params": {"annotation": 2}},
        {"t": 6.0, "action": "draw_line", "params": {"series": "previous_estimate", "to": "end", "duration": 2}},
    ],
    "line_split": [
        {"t": 0.0, "action": "reveal_line", "params": {"series": "previous_estimate"}},
        {"t": 1.0, "action": "split_line", "params": {"series": "revised_estimate", "style": "peel", "duration": 2}},
        {"t": 3.5, "action": "shade_gap", "params": {"series": "revised_estimate"}},
        {"t": 4.0, "action": "label_gap", "params": {"series": "revised_estimate", "x": "end"}},
    ],
}


@pytest.mark.parametrize("template", ["line_draw", "line_split"])
def test_seek_is_pure_function_of_time(tmp_path, template):
    page_path = make_page(tmp_path, template, CUES[template])
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1080, "height": 1152})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(page_path.as_uri())
        pg.wait_for_function("window.__ready === true")
        shot = lambda t: (pg.evaluate("t => window.__seek(t)", t), pg.screenshot())
        sig_a, img_a = shot(5.3)
        shot(9.0)
        shot(0.0)
        sig_b, img_b = shot(5.3)
        assert sig_a == sig_b and img_a == img_b
        # nothing animates after the last cue settles -> frames can be reused
        assert shot(20.0)[0] == shot(30.0)[0]
        # and something does change while a line is drawing
        assert shot(1.5)[0] != shot(2.0)[0]
        b.close()
        assert not errors
