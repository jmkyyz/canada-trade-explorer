"""Template registry. To add a template, import its class and add it here
(and add static/js/templates/<name>.js). Planned: bars_sorted,
scatter_zoom, big_number."""
from .line_draw import LineDraw
from .line_split import LineSplit

REGISTRY = {t.name: t for t in (LineDraw, LineSplit)}


def get(name):
    if name not in REGISTRY:
        raise KeyError(f"Unknown template '{name}'")
    return REGISTRY[name]


def manifests():
    return [t.manifest() for t in REGISTRY.values()]
