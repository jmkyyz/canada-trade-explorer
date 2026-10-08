"""Theme loading. A theme is a folder in themes/:

  theme.json   all colours, fonts, sizes, layout regions, caption style
  theme.css    optional, injected into the chart page (@font-face etc.)
  fonts/       optional, font files for theme.css and for libass captions
"""
import json
from pathlib import Path

from . import config


def theme_dir(name: str) -> Path:
    d = (config.THEMES_DIR / name).resolve()
    if not (d / "theme.json").exists() or config.THEMES_DIR.resolve() not in d.parents:
        raise FileNotFoundError(f"Theme '{name}' not found in {config.THEMES_DIR}")
    return d


def load_theme(name: str = None) -> dict:
    name = name or config.DEFAULT_THEME
    d = theme_dir(name)
    theme = json.loads((d / "theme.json").read_text())
    theme["_name"] = name
    return theme


def theme_css(name: str) -> str:
    p = theme_dir(name) / "theme.css"
    return p.read_text() if p.exists() else ""


def fonts_dir(name: str) -> Path | None:
    p = theme_dir(name) / "fonts"
    return p if p.is_dir() else None


def list_themes() -> list[str]:
    return sorted(p.parent.name for p in config.THEMES_DIR.glob("*/theme.json"))
