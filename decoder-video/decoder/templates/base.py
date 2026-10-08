"""Template base class and the action vocabulary shared by every template.

A chart template is two halves with the same name:

  decoder/templates/<name>.py      manifest: config defaults, the actions it
                                   understands and their parameters, data checks
  static/js/templates/<name>.js    renderer: draws the chart *as a pure function
                                   of time* from the compiled cue timeline

The editor builds its cue forms from ``actions`` below, so adding a template
means writing those two files and registering the Python class in
``templates/__init__.py``. Nothing else in the app changes.

Parameter types understood by the editor and validator:
  series      key of a configured series
  x           an x value from the data (string), or "end" when allow_end
  annotation  id of a configured annotation
  number      float
  int         integer
  bool        checkbox
  text        free text
  choice      one of ``options``
"""
from dataclasses import dataclass, field


@dataclass
class Param:
    name: str
    type: str
    label: str
    default: object = None
    options: list = field(default_factory=list)
    allow_end: bool = False
    optional: bool = False

    def to_json(self):
        d = {"name": self.name, "type": self.type, "label": self.label, "default": self.default}
        if self.options:
            d["options"] = self.options
        if self.allow_end:
            d["allow_end"] = True
        if self.optional:
            d["optional"] = True
        return d


@dataclass
class Action:
    name: str
    label: str
    description: str
    params: list = field(default_factory=list)

    def to_json(self):
        return {"name": self.name, "label": self.label, "description": self.description,
                "params": [p.to_json() for p in self.params]}


def duration(default=0.6, label="Duration (s)"):
    return Param("duration", "number", label, default)


EASES = ["cubic-in-out", "linear", "quad-out", "cubic-out"]

COMMON_ACTIONS = [
    Action("show_chart", "Show chart",
           "Fade in title, axes and source. If no project has this cue the chart "
           "frame is visible from the first frame.",
           [duration(0.6)]),
    Action("hide_chart", "Hide chart", "Fade the whole chart out.", [duration(0.6)]),
    Action("show_annotation", "Show annotation", "Fade in a configured annotation.",
           [Param("annotation", "annotation", "Annotation"), duration(0.5)]),
    Action("hide_annotation", "Hide annotation", "Fade out an annotation.",
           [Param("annotation", "annotation", "Annotation"), duration(0.4)]),
    Action("hold", "Hold",
           "No change on screen. A marker for the cue sheet (e.g. 'let this sink in').",
           [Param("note", "text", "Note for the video team", "", optional=True)]),
]


class Template:
    name = "base"
    label = "Base"
    description = ""
    actions: list = []
    min_series = 1
    max_series = 8

    # -- manifest ------------------------------------------------------------
    @classmethod
    def all_actions(cls):
        return cls.actions + COMMON_ACTIONS  # template's own first: they're the common picks

    @classmethod
    def action(cls, name):
        for a in cls.all_actions():
            if a.name == name:
                return a
        return None

    @classmethod
    def manifest(cls):
        return {"name": cls.name, "label": cls.label, "description": cls.description,
                "min_series": cls.min_series, "max_series": cls.max_series,
                "actions": [a.to_json() for a in cls.all_actions()],
                "options": cls.option_fields()}

    @classmethod
    def option_fields(cls):
        """Template-specific config fields (shown in the chart config form)."""
        return []

    # -- config --------------------------------------------------------------
    @classmethod
    def default_config(cls, data: dict, theme: dict) -> dict:
        palette = theme["chart"]["series_palette"]
        cols = list(data["columns"])[: cls.max_series]
        return {
            "title": "",
            "subtitle": "",
            "source": "Source: ",
            "x_label": "",
            "y_label": "",
            "y_format": ",~s",
            "value_format": ",.0f",
            "y_min": None,
            "y_max": None,
            "y_zero": True,
            "series": [
                {"key": c, "column": c, "name": c, "color": palette[i % len(palette)],
                 "dash": False}
                for i, c in enumerate(cols)
            ],
            "annotations": [],
            "options": {f["name"]: f.get("default") for f in cls.option_fields()},
        }

    @classmethod
    def validate_config(cls, cfg: dict, data: dict) -> list[str]:
        errors = []
        keys = [s.get("key") for s in cfg.get("series", [])]
        if len(keys) < cls.min_series:
            errors.append(f"{cls.label} needs at least {cls.min_series} series")
        if len(keys) != len(set(keys)):
            errors.append("Series keys must be unique")
        for s in cfg.get("series", []):
            if s.get("column") not in data["columns"]:
                errors.append(f"Series '{s.get('key')}' points at missing column '{s.get('column')}'")
        xs = set(data["x"]["values"])
        for a in cfg.get("annotations", []):
            if a.get("x") not in xs:
                errors.append(f"Annotation {a.get('id')} x '{a.get('x')}' is not in the data")
        return errors

    # -- cues ----------------------------------------------------------------
    @classmethod
    def validate_cue(cls, action: str, params: dict, cfg: dict, data: dict) -> tuple[dict, list]:
        """Return (clean_params, errors)."""
        spec = cls.action(action)
        if not spec:
            return params, [f"Unknown action '{action}' for template {cls.name}"]
        series = {s["key"] for s in cfg.get("series", [])}
        anns = {a["id"] for a in cfg.get("annotations", [])}
        xs = data["x"]["values"]
        clean, errors = {}, []
        for p in spec.params:
            v = params.get(p.name, p.default)
            if v in (None, "") and p.optional:
                continue
            try:
                if p.type == "series":
                    v = v or (sorted(series)[0] if series else None)
                    if v not in series:
                        errors.append(f"{p.label}: unknown series '{v}'")
                elif p.type == "x":
                    if v in (None, ""):
                        v = "end" if p.allow_end else xs[-1]
                    if v != "end" and v not in xs:
                        errors.append(f"{p.label}: '{v}' is not an x value in the data")
                    if v == "end" and not p.allow_end:
                        errors.append(f"{p.label}: pick an x value")
                elif p.type == "annotation":
                    v = int(v)
                    if v not in anns:
                        errors.append(f"{p.label}: no annotation {v}")
                elif p.type == "number":
                    v = float(v)
                elif p.type == "int":
                    v = int(v)
                elif p.type == "bool":
                    v = v in (True, "true", "1", 1, "on")
                elif p.type == "choice":
                    if v not in p.options:
                        errors.append(f"{p.label}: must be one of {p.options}")
                else:
                    v = "" if v is None else str(v)
            except (TypeError, ValueError):
                errors.append(f"{p.label}: invalid value {v!r}")
            clean[p.name] = v
        return clean, errors

    @classmethod
    def describe_cue(cls, action: str, params: dict, cfg: dict) -> str:
        """Plain-English description for the cue sheet."""
        names = {s["key"]: s["name"] for s in cfg.get("series", [])}
        anns = {a["id"]: a["text"] for a in cfg.get("annotations", [])}
        p = params
        s = names.get(p.get("series"), p.get("series"))
        match action:
            case "show_chart":
                return "Show chart frame (title, axes, source)"
            case "hide_chart":
                return "Hide chart"
            case "show_annotation":
                return f"Show annotation {p['annotation']}: “{anns.get(p['annotation'], '')}”"
            case "hide_annotation":
                return f"Hide annotation {p['annotation']}"
            case "hold":
                return "Hold" + (f" — {p['note']}" if p.get("note") else "")
        return f"{action} {s or ''}".strip()
