from .base import EASES, Action, Param, Template, duration


class LineDraw(Template):
    name = "line_draw"
    label = "Line draw"
    description = "One or more time-series lines that draw themselves left to right."
    min_series = 1
    max_series = 4  # beyond four lines a vertical phone chart stops being readable

    actions = [
        Action("draw_line", "Draw line",
               "Draw a line up to an x value (or the end). It continues from where it last "
               "stopped, so several cues draw it in stages; 'Start from' begins it somewhere "
               "else instead (e.g. a second line that starts where the first one splits).",
               [Param("series", "series", "Line"),
                Param("from", "x", "Start from", "", optional=True,
                      placeholder="where the line last stopped"),
                Param("to", "x", "Draw up to", "end", allow_end=True),
                duration(2.0),
                Param("ease", "choice", "Easing", "cubic-in-out", options=EASES)]),
        Action("reveal_line", "Reveal line",
               "Fade a whole line in at once (no drawing).",
               [Param("series", "series", "Line"), duration(0.6)]),
        Action("hide_line", "Hide line", "Fade a line out.",
               [Param("series", "series", "Line"), duration(0.5)]),
        Action("highlight_point", "Highlight point",
               "Pulse a point on a line and leave a marker on it, optionally with its value.",
               [Param("series", "series", "Line"),
                Param("x", "x", "At"),
                Param("label", "bool", "Show value", True),
                Param("pulses", "int", "Pulses", 2)]),
        Action("clear_highlights", "Clear highlights", "Remove all point highlights.",
               [duration(0.4)]),
    ]

    @classmethod
    def option_fields(cls):
        return [
            {"name": "end_labels", "type": "bool", "label": "Label lines at their tip", "default": True},
            {"name": "show_dots", "type": "bool", "label": "Dot at the drawing tip", "default": True},
        ]

    @classmethod
    def describe_cue(cls, action, params, cfg):
        names = {s["key"]: s["name"] for s in cfg.get("series", [])}
        s = names.get(params.get("series"), params.get("series"))
        match action:
            case "draw_line":
                to = "the end" if params.get("to") == "end" else params.get("to")
                start = f" from {params['from']}" if params.get("from") else ""
                return f"Draw “{s}”{start} up to {to} over {params.get('duration')}s"
            case "reveal_line":
                return f"Reveal “{s}”"
            case "hide_line":
                return f"Hide “{s}”"
            case "highlight_point":
                return f"Highlight “{s}” at {params.get('x')}"
            case "clear_highlights":
                return "Clear highlights"
        return super().describe_cue(action, params, cfg)
