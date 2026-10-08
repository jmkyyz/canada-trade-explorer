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
  text        free text (one line)
  textarea    free text; hard returns are kept as line breaks
  color       #rrggbb
  choice      one of ``options``

A default of "@theme:<path>" (e.g. "@theme:overlay.text_size") means "use the
theme's value": the editor pre-fills it from the theme, and if the cue leaves
it out the renderer falls back to the theme, so a rebrand changes it too.
"""
import re
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
    placeholder: str = ""  # editor label for "left blank" on optional params

    def to_json(self):
        d = {"name": self.name, "type": self.type, "label": self.label, "default": self.default}
        if self.options:
            d["options"] = self.options
        if self.allow_end:
            d["allow_end"] = True
        if self.optional:
            d["optional"] = True
        if self.placeholder:
            d["placeholder"] = self.placeholder
        return d


@dataclass
class Action:
    name: str
    label: str
    description: str
    params: list = field(default_factory=list)
    group: str = "Chart"  # heading in the editor's action menu

    def to_json(self):
        return {"name": self.name, "label": self.label, "description": self.description,
                "group": self.group, "params": [p.to_json() for p in self.params]}


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
           [Param("note", "text", "Note for the video team", "", optional=True)], group="Other"),
]

TEXT_SLOTS = ["1", "2", "3", "4", "5", "6"]

# Big text over the chart. Text lives in numbered slots: showing text in a slot
# that's already filled replaces what's there, in the same place.
OVERLAY_ACTIONS = [
    Action("dim_chart", "Fade chart back",
           "Fade the chart into the background so text can sit on top of it.",
           [Param("amount", "number", "How far back (0 = not at all, 1 = gone)", "@theme:overlay.dim"),
            Param("color", "color", "Fade towards colour", "@theme:overlay.scrim_color"),
            duration(0.6)], group="Text over chart"),
    Action("undim_chart", "Bring chart back", "Undo 'Fade chart back'.",
           [duration(0.6)], group="Text over chart"),
    Action("show_text", "Show text",
           "Put text over the chart; hard returns make new lines. Slots 1, 2 and 3 start as a "
           "label, a big number and a note. Showing text in a slot that already has text "
           "replaces it in place; the 'count' effect rolls the number from the old text to "
           "the new one (e.g. 1.6% to 0.6%).",
           [Param("slot", "choice", "Slot", "1", options=TEXT_SLOTS),
            Param("text", "textarea", "Text"),
            Param("size", "number", "Font size (px)", "@theme:overlay.text_size"),
            Param("color", "color", "Colour", "@theme:overlay.text_color"),
            Param("weight", "choice", "Weight", "bold", options=["bold", "regular"]),
            Param("y", "number", "Vertical position (% of chart height, 0 = top)", 50),
            Param("align", "choice", "Align", "center", options=["center", "left", "right"]),
            Param("effect", "choice", "Effect", "fade", options=["fade", "rise", "count"]),
            duration(0.5, "Duration (s); for 'count', how long the number rolls")],
           group="Text over chart"),
    Action("hide_text", "Hide text", "Fade out the text in one slot, or in every slot.",
           [Param("slot", "choice", "Slot", "all", options=["all"] + TEXT_SLOTS), duration(0.4)],
           group="Text over chart"),
]

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


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
        return cls.actions + COMMON_ACTIONS + OVERLAY_ACTIONS  # template's own first

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
            if isinstance(v, str) and v.startswith("@theme:"):
                continue  # left out: the renderer uses the theme's value
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
                elif p.type == "color":
                    if not HEX.match(str(v)):
                        errors.append(f"{p.label}: use a colour like #1a2b3c")
                elif p.type == "textarea":
                    v = str(v or "").replace("\r\n", "\n").strip("\n")
                    if not v.strip():
                        errors.append(f"{p.label}: enter some text")
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
            case "dim_chart":
                return "Fade chart back"
            case "undim_chart":
                return "Bring chart back"
            case "show_text":
                txt = " / ".join(ln.strip() for ln in p.get("text", "").split("\n") if ln.strip())
                how = ", counting" if p.get("effect") == "count" else ""
                return f"Text slot {p.get('slot')}{how}: “{txt}”"
            case "hide_text":
                return "Hide all text" if p.get("slot") == "all" else f"Hide text slot {p.get('slot')}"
        return f"{action} {s or ''}".strip()
