#!/usr/bin/env python3
"""CIMT Trade Explorer — dimension lookup parser (Phase 1).

The bulk zips ship fixed-width, **Latin-1** lookup files mapping each code to a
bilingual description with a validity date range (codes get redefined over time,
so a code may appear in several rows with different valid_from/valid_to). This
parses them into clean Parquet lookups for the query API/UI.

Fixed-width layout (0-indexed slices, verified Phase 0 2026-06-26):
  HS files  (ODPF_1/2/3/4): code[0:10] from[11:17] to[18:24] uom[25:28]
                             en[29:112] fr[112:195]
  plain     (ODPF_5/6/7/8/9): code[0:10] from[11:17] to[18:24]
                             en[25:108]  fr[108:191]
Compound codes: country = alpha[0:2] + numeric[3:6]; province = numeric[0:2] +
abbrev[3:5] (abbrev matches the data files' province column, e.g. ON/BC/QC).

File map (same ODPF_* names in every zip):
  ODPF_1 HS10 (imports)   ODPF_2 HS8 (exports)
  ODPF_3 HS6 import (M)   ODPF_4 HS6 export (X)
  ODPF_5 HS2 (shared)     ODPF_6 Country   ODPF_7 State (US/foreign)
  ODPF_8 Province (CA)    ODPF_9 Unit of measure
Note: no HS4 description file is shipped — HS4 ("heading") labels are a known
gap to source elsewhere if the UI needs them.

Writes Parquet under cimt/data/parquet/dim/:
  dim_hs.parquet (classification M/X/both, hs, hs_len, uom, en, fr, valid_*)
  dim_country / dim_province / dim_state / dim_uom .parquet

Usage:
  python dimensions.py --year 2025    # use that year's already-downloaded zips
  python dimensions.py                # newest year present in data/raw
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests

# StatCan ships no HS4 ("heading") labels — pull the standard WCO HS4 headings
# from this public-domain dataset so the product tree isn't bare at that level.
HS4_SOURCE = ("https://raw.githubusercontent.com/datasets/harmonized-system/"
              "master/data/harmonized-system.csv")

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"
DIM = HERE / "data" / "parquet" / "dim"


def _read(zf: zipfile.ZipFile, key: str) -> list[str]:
    """Decode the ODPF_<key>_*.TXT member as Latin-1 → non-empty lines."""
    matches = [n for n in zf.namelist()
               if Path(n).name.upper().startswith(f"ODPF_{key}_")]
    if not matches:
        return []
    text = zf.open(matches[0]).read().decode("iso-8859-1")
    return [ln for ln in text.splitlines() if ln.strip()]


def _parse(lines, has_uom, code_slice=(0, 10)):
    """Yield dicts for one fixed-width file."""
    en0, fr0, fr1 = (29, 112, 195) if has_uom else (25, 108, 191)
    for ln in lines:
        rec = {
            "code": ln[code_slice[0]:code_slice[1]].strip(),
            "valid_from": ln[11:17].strip(),
            "valid_to": ln[18:24].strip(),
            "en": ln[en0:fr0].strip(),
            "fr": ln[fr0:fr1].strip(),
        }
        if has_uom:
            rec["uom"] = ln[25:28].strip() or None
        yield rec


def fetch_hs4_labels() -> list[dict]:
    """WCO HS4 heading labels (classification 'both'), or [] if unreachable."""
    try:
        text = requests.get(HS4_SOURCE, timeout=30).text
    except Exception as e:  # network optional — degrade gracefully
        print(f"  ! HS4 labels: {HS4_SOURCE} unreachable ({e}); tree shows bare codes")
        return []
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("level") == "4":
            out.append({"classification": "both", "hs": row["hscode"],
                        "hs_len": 4, "uom": None, "en": row["description"],
                        "fr": None, "valid_from": "", "valid_to": "999912"})
    print(f"  · HS4 labels: {len(out):,} headings from WCO dataset")
    return out


def build(year: int) -> None:
    imp_zip = RAW / f"CIMT-CICM_Imp_{year}.zip"
    exp_zip = RAW / f"CIMT-CICM_Tot_Exp_{year}.zip"
    if not imp_zip.exists():
        raise FileNotFoundError(f"{imp_zip} — run ingest.py first")
    DIM.mkdir(parents=True, exist_ok=True)

    def write(name: str, records: list[dict]):
        if not records:
            print(f"  ! {name}: no records (file missing)")
            return
        cols = list(records[0].keys())
        table = pa.table({c: pa.array([r.get(c) for r in records],
                                      type=pa.string() if c != "hs_len"
                                      else pa.int8()) for c in cols})
        out = DIM / f"{name}.parquet"
        pq.write_table(table, out)
        print(f"  · {name}: {len(records):,} rows -> {out.name}")

    with zipfile.ZipFile(imp_zip) as zi:
        # HS dimension: HS10(M) + HS6(M) + HS2(both) from imports …
        hs: list[dict] = []
        for key, cls in [("1", "M"), ("3", "M"), ("5", "both")]:
            for r in _parse(_read(zi, key), has_uom=(key in ("1", "3"))):
                r2 = {"classification": cls, "hs": r["code"],
                      "hs_len": len(r["code"]), "uom": r.get("uom"),
                      "en": r["en"], "fr": r["fr"],
                      "valid_from": r["valid_from"], "valid_to": r["valid_to"]}
                hs.append(r2)
        # Country (alpha + numeric), State, Province (numeric + abbrev), UoM
        country = [{"code": ln[0:2].strip(), "code_num": ln[3:6].strip(),
                    "valid_from": ln[11:17].strip(), "valid_to": ln[18:24].strip(),
                    "en": ln[25:108].strip(), "fr": ln[108:191].strip()}
                   for ln in _read(zi, "6")]
        state = list(_parse(_read(zi, "7"), has_uom=False, code_slice=(0, 2)))
        province = [{"code_num": ln[0:2].strip(), "code": ln[3:5].strip(),
                     "valid_from": ln[11:17].strip(), "valid_to": ln[18:24].strip(),
                     "en": ln[25:108].strip(), "fr": ln[108:191].strip()}
                    for ln in _read(zi, "8")]
        uom = list(_parse(_read(zi, "9"), has_uom=False, code_slice=(0, 10)))

    # … plus export-side HS classification (HS8 = X, HS6X = X) if available.
    if exp_zip.exists():
        with zipfile.ZipFile(exp_zip) as ze:
            for key in ("2", "4"):
                for r in _parse(_read(ze, key), has_uom=True):
                    hs.append({"classification": "X", "hs": r["code"],
                               "hs_len": len(r["code"]), "uom": r.get("uom"),
                               "en": r["en"], "fr": r["fr"],
                               "valid_from": r["valid_from"],
                               "valid_to": r["valid_to"]})
    else:
        print(f"  ! {exp_zip.name} not present — HS8/HS6X export labels skipped")

    hs += fetch_hs4_labels()  # fill the HS4 gap (StatCan ships none)

    print(f"Dimensions from {year}:")
    write("dim_hs", hs)
    write("dim_country", country)
    write("dim_state", state)
    write("dim_province", province)
    write("dim_uom", uom)


def newest_year() -> int:
    years = sorted(int(re.search(r"_(\d{4})\.zip$", p.name).group(1))
                   for p in RAW.glob("CIMT-CICM_Imp_*.zip"))
    if not years:
        raise FileNotFoundError(f"no import zips in {RAW}")
    return years[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse CIMT dimension lookups")
    ap.add_argument("--year", type=int, help="year whose zips to read "
                    "(default: newest present in data/raw)")
    args = ap.parse_args()
    build(args.year or newest_year())
    return 0


if __name__ == "__main__":
    sys.exit(main())
