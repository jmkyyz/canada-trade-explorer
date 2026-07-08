#!/usr/bin/env python3
"""CIMT Trade Explorer — publish a trimmed slice to Cloudflare R2 (Phase 5).

The full 20-year, HS10-detail store stays local. For the always-on online copy
(coworkers, no local data) we publish a much smaller slice — the last N years at
the HS2/HS4/HS6 rollup tiers only — to Cloudflare R2 object storage. Smaller =
faster remote queries; storage is pennies either way. The app prefers a full
local store and falls back to this slice when there isn't one.

Two steps:
  python publish.py --stage             # build data/publish/parquet locally
  python publish.py --upload            # sync it to R2 (needs creds, see below)
  python publish.py --stage --upload    # both

What's included: tiers hs2/hs4/hs6 for year >= (latest - YEARS + 1), plus the
dimension lookups. Excluded: the hs8/HS10 detail tiers (local-only).

R2 credentials (env vars, S3-compatible — create a bucket + API token in the
Cloudflare dashboard first):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
  R2_PREFIX (optional, default "cimt")
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARQUET = HERE / "data" / "parquet"
PUBLISH = HERE / "data" / "publish" / "parquet"

ONLINE_TIERS = ["hs2", "hs4", "hs6"]   # HS6 is the deepest online level
DEFAULT_YEARS = 10


def latest_year() -> int:
    years = []
    for d in (PARQUET / "hs6").glob("flow=*/year=*"):
        try:
            years.append(int(d.name.split("=")[1]))
        except (IndexError, ValueError):
            pass
    if not years:
        sys.exit(f"no data under {PARQUET/'hs6'} — run ingest first")
    return max(years)


def flows_present() -> list[str]:
    return sorted(d.name.split("=")[1]
                  for d in (PARQUET / "hs6").glob("flow=*") if d.is_dir())


def stage(years: int) -> None:
    """Build the online slice, optimized for *remote* reading by DuckDB-WASM.

    Per-year files with tiny row-groups force hundreds of HTTP range requests
    over the internet (~the whole latency budget). Instead we write ONE file per
    tier+flow with few large row-groups, drop the `state` dimension (unused by
    the UI, ~45% of the rows), sort by year/hs so filtered queries prune, and
    zstd-compress. That turns a multi-second/minute remote query into a handful
    of requests.
    """
    import duckdb
    cutoff = latest_year() - years + 1
    if PUBLISH.exists():
        shutil.rmtree(PUBLISH)
    PUBLISH.mkdir(parents=True)
    con = duckdb.connect()
    nfiles = 0
    for tier in ONLINE_TIERS:
        (PUBLISH / tier).mkdir(parents=True, exist_ok=True)
        for flow in flows_present():
            src_dir = PARQUET / tier / f"flow={flow}"
            if not src_dir.exists():
                continue
            out = PUBLISH / tier / f"{flow}.parquet"
            con.execute(f"""
                COPY (
                  SELECT year, month, hs, country, province,
                         sum(value) AS value,
                         CASE WHEN count(DISTINCT uom)=1 THEN sum(quantity) END AS quantity,
                         CASE WHEN count(DISTINCT uom)=1 THEN any_value(uom) END AS uom
                  FROM read_parquet('{src_dir.as_posix()}/**/*.parquet',
                                    hive_partitioning=true)
                  WHERE year >= {cutoff}
                  GROUP BY year, month, hs, country, province
                  ORDER BY year, hs
                ) TO '{out.as_posix()}'
                (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 1000000)
            """)
            nfiles += 1
    # dimension lookups (small; needed for labels + cheap drill-down)
    if (PARQUET / "dim").exists():
        shutil.copytree(PARQUET / "dim", PUBLISH / "dim")
    write_manifest()
    size = sum(f.stat().st_size for f in PUBLISH.rglob("*") if f.is_file())
    print(f"staged {nfiles} consolidated files, years {cutoff}+, tiers "
          f"{'/'.join(ONLINE_TIERS)} (state dropped) -> {PUBLISH}")
    print(f"slice size: {size/1e9:.2f} GB")


def write_manifest() -> None:
    """Index the slice for the browser (object storage has no directory listing).
    Layout is one file per tier+flow: <tier>/<flow>.parquet, with year a column."""
    import duckdb
    con = duckdb.connect()
    src = f"read_parquet('{(PUBLISH/'hs6').as_posix()}/*.parquet')"
    pmin, pmax = con.execute(
        f"SELECT min(year*100+month), max(year*100+month) FROM {src}").fetchone()
    years = [r[0] for r in con.execute(
        f"SELECT DISTINCT year FROM {src} ORDER BY year").fetchall()]
    flows = sorted(p.stem for p in (PUBLISH / "hs6").glob("*.parquet"))
    dims = sorted(p.stem for p in (PUBLISH / "dim").glob("*.parquet"))
    manifest = {
        "layout": "consolidated",   # <tier>/<flow>.parquet, year is a column
        "tiers": ONLINE_TIERS,
        "flows": flows,
        "years": years,
        "period_min": pmin,
        "period_max": pmax,
        "dims": dims,
        "detail": False,            # online slice has no HS8/HS10 detail or state
    }
    (PUBLISH / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"manifest: {len(flows)} flows, years {years[0]}–{years[-1]}, "
          f"period {pmin}–{pmax}")


def upload(prefix: str) -> None:
    try:
        import boto3
    except ImportError:
        sys.exit("pip install boto3 to upload to R2")
    need = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing R2 env vars: {missing}")
    if not PUBLISH.exists():
        sys.exit("nothing staged — run with --stage first")
    # Tolerate R2_ACCOUNT_ID being pasted as the full endpoint URL — pull the
    # 32-hex account id out of whatever was provided.
    acct = os.environ["R2_ACCOUNT_ID"].strip()
    m = re.search(r"[0-9a-fA-F]{32}", acct)
    acct = m.group(0) if m else acct
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = os.environ["R2_BUCKET"]
    # Sync: remove existing objects under the prefix first, so a layout change
    # (e.g. old per-year files) doesn't leave stale objects behind.
    old = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"{prefix}/"):
        old += [{"Key": o["Key"]} for o in page.get("Contents", [])]
    for i in range(0, len(old), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": old[i:i+1000]})
    if old:
        print(f"removed {len(old)} existing objects under {prefix}/")
    n = 0
    for f in PUBLISH.rglob("*"):
        if not f.is_file():
            continue
        key = f"{prefix}/{f.relative_to(PUBLISH).as_posix()}"
        # Browsers apply heuristic caching when no Cache-Control is set, which
        # served day-old manifests after a release. The manifest must always be
        # revalidated; parquet files are fetched with a ?v= cache-buster so a
        # short TTL is safe.
        cache = ("no-cache" if f.name == "manifest.json"
                 else "public, max-age=3600")
        s3.upload_file(str(f), bucket, key,
                       ExtraArgs={"CacheControl": cache})
        n += 1
    print(f"uploaded {n} files to r2://{bucket}/{prefix}/")


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish trimmed CIMT slice to R2")
    ap.add_argument("--stage", action="store_true", help="build the slice locally")
    ap.add_argument("--upload", action="store_true", help="sync the slice to R2")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS,
                    help=f"years of history to include (default {DEFAULT_YEARS})")
    args = ap.parse_args()
    if not (args.stage or args.upload):
        ap.error("pass --stage and/or --upload")
    if args.stage:
        stage(args.years)
    if args.upload:
        upload(os.environ.get("R2_PREFIX", "cimt"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
