#!/usr/bin/env python3
"""CIMT Trade Explorer — Phase 1 ingest pipeline.

Downloads StatCan CIMT bulk annual zips, parses the HS-detail CSV out of each,
and writes a Hive-partitioned DuckDB/Parquet store plus pre-computed HS rollups
so the query API (Phase 2) gets sub-second aggregations with no per-vector WDS
round-trips.

Data source (verified Phase 0, 2026-06-26):
  https://www150.statcan.gc.ca/n1/pub/71-607-x/2021004/zip/CIMT-CICM_<FLOW>_<YEAR>.zip
  Same /2021004/ path for every year 2007..present. Per-year zip holds:
    - one HS-detail CSV (imports = HS10, exports = HS8)
    - StatCan's own HS6 / HS2 rollup CSVs (used only as a correctness cross-check)
    - fixed-width Latin-1 dimension lookup .TXT files (parsed by dimensions.py)
  Data CSVs are UTF-8 w/ CRLF; columns:
    YearMonth, HS, Country, Province, State, Value[, Quantity, UoM]

Layout written under cimt/data/parquet/:
  detail/flow=<flow>/year=<yyyy>/data.parquet   full HS10 (imp) / HS8 (exp)
  hs8|hs6|hs4|hs2/flow=<flow>/year=<yyyy>/...    pre-computed rollups

Rollups: value is always summed. Quantity is summed ONLY where every child row
shares one unit of measure (2.6% of HS6 codes mix UoM); otherwise quantity/uom
are nulled rather than summing incomparable units.

Usage:
  python ingest.py --years 2023                 # one year, all flows
  python ingest.py --years 2007-2026 --flows imp,exp_tot,exp_dom
  python ingest.py --years 2023 --no-download   # use already-downloaded zips
  python ingest.py --years 2025 --keep-raw      # keep unzipped CSVs after ingest
"""
from __future__ import annotations

import argparse
import io
import shutil
import sys
import time
import zipfile
from pathlib import Path

import duckdb
import requests

# --- paths -----------------------------------------------------------------
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RAW = DATA / "raw"
PARQUET = DATA / "parquet"

BASE_URL = "https://www150.statcan.gc.ca/n1/pub/71-607-x/2021004/zip"

# CLI flow name -> (zip filename stem, partition value)
FLOWS = {
    "imp": "CIMT-CICM_Imp",
    "exp_tot": "CIMT-CICM_Tot_Exp",
    "exp_dom": "CIMT-CICM_Dom_Exp",
}

# Rollup tiers (HS digit length) to materialize per flow, beyond the native
# detail level. Both flows get an HS8 tier so imports (HS10) and exports (HS8)
# share a comparable browsing level.
ROLLUP_TIERS = [8, 6, 4, 2]


# --- download / unzip ------------------------------------------------------
def zip_url(flow: str, year: int) -> str:
    return f"{BASE_URL}/{FLOWS[flow]}_{year}.zip"


def download(flow: str, year: int, force: bool = False) -> Path:
    """Fetch the year's zip into data/raw (skip if already present)."""
    RAW.mkdir(parents=True, exist_ok=True)
    dest = RAW / f"{FLOWS[flow]}_{year}.zip"
    if dest.exists() and not force:
        print(f"  · zip cached: {dest.name} ({dest.stat().st_size/1e6:.0f} MB)")
        return dest
    url = zip_url(flow, year)
    print(f"  · downloading {url}")
    t0 = time.time()
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(".zip.part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp.rename(dest)
    print(f"    {dest.stat().st_size/1e6:.0f} MB in {time.time()-t0:.0f}s")
    return dest


def classify_header(header: str) -> list[str]:
    """Map each CSV column to a canonical name by its (bilingual) header token.

    Robust to the import/export asymmetry: imports carry a Province column,
    exports do not; both carry State. Returns a list aligned to the columns,
    e.g. ['ym','hs','country','province','state','value','quantity','uom'].
    """
    out = []
    for tok in header.rstrip("\r\n").split(","):
        t = tok.strip().lower()
        if t.startswith("yearmonth"):
            out.append("ym")
        elif t.startswith("hs"):
            out.append("hs")
        elif t.startswith("country"):
            out.append("country")
        elif t.startswith("province"):
            out.append("province")
        elif t.startswith("state"):
            out.append("state")
        elif t.startswith("value"):
            out.append("value")
        elif t.startswith("quantity"):
            out.append("quantity")
        elif t.startswith("unit of measure"):
            out.append("uom")
        else:
            out.append(f"_extra{len(out)}")
    return out


def find_detail_csv(zf: zipfile.ZipFile, tmpdir: Path) -> tuple[Path, list[str]]:
    """Extract the HS-*detail* CSV (longest HS code) and return its column map.

    Identifies the detail file by header rather than by ODPFN file number, so it
    survives StatCan renumbering files across the four year-range datasets.
    """
    best: tuple[int, str, list[str]] | None = None
    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        with zf.open(name) as fh:
            header = io.TextIOWrapper(fh, encoding="utf-8").readline()
        cmap = classify_header(header)
        if "hs" not in cmap:
            continue
        # HS header token is "HS10"/"HS8"/... — longest digits = detail level.
        cols = header.split(",")
        hs = "".join(c for c in cols[cmap.index("hs")] if c.isdigit())
        digits = int(hs) if hs else 0
        if best is None or digits > best[0]:
            best = (digits, name, cmap)
    if best is None:
        raise RuntimeError("no HS-detail CSV found in zip")
    out = tmpdir / Path(best[1]).name
    out.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(best[1]) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out, best[2]


# --- parquet write ---------------------------------------------------------
def detail_relation(con: duckdb.DuckDBPyConnection, csv_path: Path,
                    cmap: list[str]):
    """A normalized relation over one detail CSV with the unified schema:
    month, hs, country, province, state, value, quantity, uom.

    `cmap` maps physical columns to canonical names (see classify_header).
    Missing canonical columns (e.g. province on exports, quantity/uom on HS2)
    are filled with NULL so every flow shares one schema.
    """
    cols = {f"column{i}": "VARCHAR" for i in range(len(cmap))}
    idx = {name: i for i, name in enumerate(cmap)}

    def col(name, cast=None, nullif_empty=False):
        if name not in idx:
            return f"CAST(NULL AS {cast or 'VARCHAR'})"
        ref = f"column{idx[name]}"
        if nullif_empty:
            ref = f"NULLIF({ref}, '')"
        return f"CAST({ref} AS {cast})" if cast else ref

    ym = f"column{idx['ym']}"
    return con.sql(f"""
        SELECT
            CAST(substr({ym}, 5, 2) AS TINYINT)          AS month,
            {col('hs')}                                   AS hs,
            {col('country')}                              AS country,
            {col('province', nullif_empty=True)}          AS province,
            {col('state', nullif_empty=True)}             AS state,
            {col('value', cast='BIGINT')}                 AS value,
            {col('quantity', cast='BIGINT')}              AS quantity,
            {col('uom', nullif_empty=True)}               AS uom
        FROM read_csv('{csv_path.as_posix()}', header=true,
                      columns={cols}, auto_detect=false)
    """)


def write_partition(con, tier: str, flow: str, year: int):
    """COPY the current `src` relation to <tier>/flow=/year=/data.parquet,
    overwriting only this flow+year partition (idempotent re-runs)."""
    out_dir = PARQUET / tier / f"flow={flow}" / f"year={year}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    con.sql(
        f"COPY src TO '{(out_dir/'data.parquet').as_posix()}' (FORMAT parquet)"
    )


def ingest_year(con, flow: str, year: int, download_first: bool, keep_raw: bool,
                force_download: bool = False):
    print(f"[{flow} {year}]")
    zip_path = download(flow, year, force=force_download) if download_first else (
        RAW / f"{FLOWS[flow]}_{year}.zip")
    if not zip_path.exists():
        raise FileNotFoundError(f"{zip_path} (run without --no-download)")

    work = RAW / f"_work_{flow}_{year}"
    if work.exists():
        shutil.rmtree(work)
    with zipfile.ZipFile(zip_path) as zf:
        csv_path, cmap = find_detail_csv(zf, work)
    print(f"  · detail CSV: {csv_path.name}  cols={cmap}")

    # Load detail into a temp table once; reuse for detail write + all rollups.
    con.execute("DROP TABLE IF EXISTS detail")
    con.sql("CREATE TABLE detail AS "
            + detail_relation(con, csv_path, cmap).sql_query())
    nrows = con.execute("SELECT count(*) FROM detail").fetchone()[0]
    hs_len = con.execute("SELECT max(length(hs)) FROM detail").fetchone()[0]
    print(f"  · {nrows:,} detail rows, HS{hs_len}")

    # Native detail partition.
    con.execute("CREATE OR REPLACE TEMP VIEW src AS SELECT * FROM detail")
    write_partition(con, "detail", flow, year)

    # Rollup tiers shorter than the native detail length.
    for tier in ROLLUP_TIERS:
        if tier >= hs_len:
            continue
        con.execute(f"""
            CREATE OR REPLACE TEMP VIEW src AS
            SELECT month, substr(hs,1,{tier}) AS hs, country, province, state,
                   sum(value) AS value,
                   CASE WHEN count(DISTINCT uom)=1 THEN sum(quantity) END AS quantity,
                   CASE WHEN count(DISTINCT uom)=1 THEN any_value(uom) END AS uom
            FROM detail
            GROUP BY month, substr(hs,1,{tier}), country, province, state
        """)
        rr = con.execute("SELECT count(*) FROM src").fetchone()[0]
        write_partition(con, f"hs{tier}", flow, year)
        print(f"    HS{tier} rollup: {rr:,} rows")

    con.execute("DROP TABLE detail")
    if not keep_raw:
        shutil.rmtree(work, ignore_errors=True)


def parse_years(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser(description="CIMT bulk ingest → Parquet store")
    ap.add_argument("--years", required=True,
                    help="e.g. 2023 or 2007-2026 or 2019,2021,2023")
    ap.add_argument("--flows", default="imp,exp_tot,exp_dom",
                    help="comma list of: imp, exp_tot, exp_dom")
    ap.add_argument("--no-download", action="store_true",
                    help="use already-downloaded zips in data/raw")
    ap.add_argument("--force-download", action="store_true",
                    help="re-download even if the zip is cached (for refresh)")
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep unzipped CSVs after ingest")
    args = ap.parse_args()

    years = parse_years(args.years)
    flows = [f.strip() for f in args.flows.split(",") if f.strip()]
    bad = [f for f in flows if f not in FLOWS]
    if bad:
        ap.error(f"unknown flow(s): {bad}; choose from {list(FLOWS)}")

    PARQUET.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    t0 = time.time()
    done = 0
    for year in years:
        for flow in flows:
            try:
                ingest_year(con, flow, year, not args.no_download, args.keep_raw,
                            force_download=args.force_download)
                done += 1
            except requests.HTTPError as e:
                print(f"  ! skip {flow} {year}: {e}")
    con.close()
    print(f"\nIngested {done} flow-years in {time.time()-t0:.0f}s "
          f"-> {PARQUET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
