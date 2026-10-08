from .base import EASES, Action, Param, duration
from .line_draw import LineDraw


def divergence_x(data: dict, a: str, b: str) -> str | None:
    """Last x where columns a and b still agree (where the split starts)."""
    xs, ca, cb = data["x"]["values"], data["columns"].get(a), data["columns"].get(b)
    if not ca or not cb:
        return None
    for i, (va, vb) in enumerate(zip(ca, cb)):
        if va is not None and vb is not None and abs(va - vb) > 1e-9 * max(1, abs(va)):
            return xs[max(0, i - 1)]
    return None


class LineSplit(LineDraw):
    name = "line_split"
    label = "Line split"
    description = ("A second series that diverges from the first, e.g. previous vs. revised "
                   "estimates. The new line peels away from the old one where they part.")
    min_series = 2
    max_series = 3

    actions = LineDraw.actions + [
        Action("split_line", "Split line off",
               "The diverging series emerges from the base line at the split point. "
               "'peel' morphs it away from the base; 'draw' draws it rightward from the split.",
               [Param("series", "series", "Diverging line", "@diverging"),
                Param("from", "x", "Split at (blank = template option)", "", optional=True),
                Param("style", "choice", "Style", "peel", options=["peel", "draw"]),
                duration(2.5),
                Param("ease", "choice", "Easing", "cubic-in-out", options=EASES)]),
        Action("shade_gap", "Shade the gap",
               "Fill the area between the base line and the diverging line.",
               [Param("series", "series", "Diverging line", "@diverging"), duration(0.8)]),
        Action("label_gap", "Label the gap",
               "Bracket and label the difference between the two lines at one x value.",
               [Param("series", "series", "Diverging line", "@diverging"),
                Param("x", "x", "At", "end", allow_end=True), duration(0.5)]),
    ]

    @classmethod
    def option_fields(cls):
        return LineDraw.option_fields() + [
            {"name": "base_series", "type": "series", "label": "Base line (the one others split from)",
             "default": None},
            {"name": "split_from", "type": "x", "label": "Split point (last shared x)", "default": None},
            {"name": "gap_format", "type": "text", "label": "Gap label format (d3-format)",
             "default": "+,.0f"},
        ]

    @classmethod
    def default_config(cls, data, theme):
        cfg = super().default_config(data, theme)
        keys = [s["key"] for s in cfg["series"]]
        cfg["options"]["base_series"] = keys[0] if keys else None
        if len(keys) > 1:
            cfg["options"]["split_from"] = divergence_x(data, keys[0], keys[1])
        return cfg

    @classmethod
    def validate_config(cls, cfg, data):
        errors = super().validate_config(cfg, data)
        opts = cfg.get("options", {})
        keys = {s["key"] for s in cfg.get("series", [])}
        if opts.get("base_series") not in keys:
            errors.append("Pick the base line in template options")
        if opts.get("split_from") and opts["split_from"] not in data["x"]["values"]:
            errors.append(f"Split point '{opts['split_from']}' is not in the data")
        return errors

    @classmethod
    def validate_cue(cls, action, params, cfg, data):
        params = dict(params)
        base = cfg.get("options", {}).get("base_series")
        if params.get("series") in (None, "", "@diverging"):
            spec = cls.action(action)
            if spec and any(p.default == "@diverging" for p in spec.params):
                params["series"] = next((s["key"] for s in cfg.get("series", []) if s["key"] != base), None)
        clean, errors = super().validate_cue(action, params, cfg, data)
        if action in ("split_line", "shade_gap", "label_gap"):
            if clean.get("series") == cfg.get("options", {}).get("base_series"):
                errors.append("Pick the diverging line, not the base line")
        return clean, errors

    @classmethod
    def describe_cue(cls, action, params, cfg):
        names = {s["key"]: s["name"] for s in cfg.get("series", [])}
        s = names.get(params.get("series"), params.get("series"))
        base = names.get(cfg.get("options", {}).get("base_series"), "base line")
        match action:
            case "split_line":
                at = params.get("from") or cfg.get("options", {}).get("split_from")
                return f"Split “{s}” off “{base}” at {at} ({params.get('style')})"
            case "shade_gap":
                return f"Shade gap between “{base}” and “{s}”"
            case "label_gap":
                x = "the end" if params.get("x") == "end" else params.get("x")
                return f"Label the gap at {x}"
        return super().describe_cue(action, params, cfg)
