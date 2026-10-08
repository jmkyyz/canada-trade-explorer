"""Captions from word timestamps: 2-4 word groups, exported as SRT (for the
video team) and ASS (styled, burned in by FFmpeg/libass).

Captions sit in the talent region (bottom of the frame) via the theme's
margin_bottom, so they never cover the chart in the top region.
"""
import re


def group_words(words: list, style: dict) -> list[dict]:
    max_w, min_w = style.get("max_words", 4), style.get("min_words", 2)
    max_chars, max_gap = style.get("max_chars", 22), style.get("max_gap", 0.6)
    groups, cur = [], []

    def flush():
        if cur:
            groups.append(list(cur))
            cur.clear()

    for w in words:
        if cur:
            prev = cur[-1]
            text_len = len(" ".join(x["text"] for x in cur + [w]))
            sentence_end = re.search(r"[.?!]['\"”]?$", prev["text"])
            clause_end = re.search(r"[,;:—]$", prev["text"])
            if (len(cur) >= max_w or text_len > max_chars or w["start"] - prev["end"] > max_gap
                    or sentence_end or (clause_end and len(cur) >= min_w)):
                flush()
        cur.append(w)
    flush()

    # Fold a lone trailing word back into the previous group when it fits.
    merged = []
    for g in groups:
        if (merged and len(g) == 1 and len(merged[-1]) < max_w
                and g[0]["start"] - merged[-1][-1]["end"] <= max_gap
                and not re.search(r"[.?!]$", merged[-1][-1]["text"])
                and len(" ".join(x["text"] for x in merged[-1] + g)) <= max_chars + 4):
            merged[-1].extend(g)
        else:
            merged.append(g)

    out = []
    for i, g in enumerate(merged):
        start, end = g[0]["start"], g[-1]["end"]
        nxt = merged[i + 1][0]["start"] if i + 1 < len(merged) else end + 1.0
        end = min(max(end + 0.25, start + 0.6), nxt)  # linger slightly, never overlap
        out.append({"start": start, "end": end, "words": g,
                    "text": " ".join(x["text"] for x in g)})
    return out


def _srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(groups: list) -> str:
    lines = []
    for i, g in enumerate(groups, 1):
        lines += [str(i), f"{_srt_time(g['start'])} --> {_srt_time(g['end'])}", g["text"], ""]
    return "\n".join(lines)


def _ass_time(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_escape(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def caption_anchor(style: dict, layout: dict) -> tuple[int, int]:
    """(ASS alignment, MarginV). 'bottom': margin_bottom up from the frame's
    bottom edge (clear of platform UI). 'seam': just below the chart, at the
    top of the talent region."""
    if style.get("position", "bottom") == "seam":
        return 8, layout["talent"]["y"] + style.get("seam_offset", 24)
    return 2, style["margin_bottom"]


def to_ass(groups: list, style: dict, canvas: dict, layout: dict) -> str:
    align, margin_v = caption_anchor(style, layout)
    up = (lambda s: s.upper()) if style.get("uppercase") else (lambda s: s)
    primary = _ass_color(style["color"])
    inline = lambda c: _ass_color(c)[4:] + "&"  # override tags take &HBBGGRR&
    on, off = inline(style.get("active_color", style["color"])), inline(style["color"])
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas['width']}
PlayResY: {canvas['height']}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Decoder,{style['font_family']},{style['size']},{primary},{primary},{_ass_color(style['outline_color'])},&H80000000,{-1 if style.get('bold') else 0},0,0,0,100,100,0,0,1,{style.get('outline', 4)},{style.get('shadow', 0)},{align},{style['margin_side']},{style['margin_side']},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for g in groups:
        words = [up(_ass_escape(w["text"])) for w in g["words"]]
        if not style.get("highlight_active_word"):
            events.append((g["start"], g["end"], " ".join(words)))
            continue
        for k, w in enumerate(g["words"]):
            s = g["start"] if k == 0 else w["start"]
            e = g["words"][k + 1]["start"] if k + 1 < len(g["words"]) else g["end"]
            if e <= s:
                continue
            text = " ".join(
                f"{{\\c&H{on}}}{x}{{\\c&H{off}}}" if j == k else x for j, x in enumerate(words))
            events.append((s, e, text))
    body = "".join(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Decoder,,0,0,0,,{t}\n"
                   for s, e, t in events)
    return header + body
