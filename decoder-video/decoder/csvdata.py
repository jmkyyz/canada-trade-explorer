"""CSV -> chart data.

The first column is the x axis; every other column is a numeric series.
x values are kept as their original strings (so cue params like
"2025-Q2" stay human-readable) plus a numeric position used for scales:

  quarter  2020-Q1, 2020Q1, 2020 Q1, Q1 2020  -> 2020.0, 2020.25, ...
  month    2020-01                          -> 2020 + (m-1)/12
  date     2020-01-15                       -> fractional year
  year     2020                             -> 2020
  number   any other numeric                -> as is
  category anything else                    -> 0, 1, 2, ...
"""
import csv
import io
import re
from datetime import date

QUARTER_RES = [
    re.compile(r"^(\d{4})\s*[-_ ]?\s*Q([1-4])$", re.I),
    re.compile(r"^Q([1-4])\s*[-_ ]?\s*(\d{4})$", re.I),
]
MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
YEAR_RE = re.compile(r"^\d{4}$")


def _quarter(s):
    m = QUARTER_RES[0].match(s)
    if m:
        return int(m[1]) + (int(m[2]) - 1) / 4
    m = QUARTER_RES[1].match(s)
    if m:
        return int(m[2]) + (int(m[1]) - 1) / 4
    return None


def _month(s):
    m = MONTH_RE.match(s)
    return int(m[1]) + (int(m[2]) - 1) / 12 if m else None


def _date(s):
    m = DATE_RE.match(s)
    if not m:
        return None
    d = date(int(m[1]), int(m[2]), int(m[3]))
    start = date(d.year, 1, 1)
    days = (date(d.year + 1, 1, 1) - start).days
    return d.year + (d - start).days / days


def _number(s):
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def detect_x_type(values):
    for kind, fn in (("quarter", _quarter), ("month", _month), ("date", _date)):
        if all(fn(v) is not None for v in values):
            return kind
    if all(YEAR_RE.match(v) for v in values):
        return "year"
    if all(_number(v) is not None for v in values):
        return "number"
    return "category"


def x_position(kind, raw, index):
    return {
        "quarter": _quarter, "month": _month, "date": _date,
        "year": lambda s: float(s), "number": _number,
    }.get(kind, lambda s: float(index))(raw)


def parse_value(s):
    s = (s or "").strip().replace("−", "-")
    if s in ("", "..", "x", "F", "NA", "n/a", "-"):
        return None
    s = s.replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_csv(text: str) -> dict:
    text = text.lstrip("﻿")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if len(rows) < 3:
        raise ValueError("CSV needs a header row and at least two data rows")
    header = [h.strip() for h in rows[0]]
    if len(header) < 2:
        raise ValueError("CSV needs an x column and at least one series column")
    body = rows[1:]
    xs = [r[0].strip() for r in body]
    kind = detect_x_type(xs)
    positions = [x_position(kind, v, i) for i, v in enumerate(xs)]
    columns = {}
    for ci, name in enumerate(header[1:], start=1):
        vals = [parse_value(r[ci] if ci < len(r) else "") for r in body]
        if all(v is None for v in vals):
            continue
        columns[name] = vals
    if not columns:
        raise ValueError("No numeric series columns found")
    return {
        "x": {"column": header[0], "type": kind, "values": xs, "positions": positions},
        "columns": columns,
    }
