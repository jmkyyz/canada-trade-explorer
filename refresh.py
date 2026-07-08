#!/usr/bin/env python3
"""CIMT Trade Explorer — monthly refresh job (Phase 4).

StatCan releases merchandise-trade data monthly, ~6 weeks in arrears. The
current-year bulk file holds every YTD month and is the only MUTABLE partition;
prior years are effectively frozen but can still see small back-revisions, so
this refreshes the current AND previous year, then rebuilds the dimension
lookups. Re-ingest is idempotent (it overwrites whole flow-year partitions), so
running this more often than data changes is harmless.

Triggered by a launchd LaunchAgent on a monthly schedule — see
com.statcan.cimt-refresh.plist and the install steps in README.md. Runs from the
TCC-safe ~/statcan-explorer clone so macOS doesn't block file access.

Manual run:  python refresh.py            # current + previous year
             python refresh.py --years 2024,2025,2026
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "data" / ".refresh.lock"
DETAIL = HERE / "data" / "parquet" / "detail"

RETRY_TRIES = 8        # with --retry: re-check this many times…
RETRY_INTERVAL = 900   # …15 min apart (~1h45m) for a late posting to land

# R2 credentials for the online publish step. Plain KEY=VALUE or a TextEdit .rtf.
R2_ENV_FILES = [Path.home() / "cimt_r2.env", Path.home() / "cimt_r2.env.rtf"]


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


def run(args: list[str]) -> int:
    """Run a sibling script with this same interpreter; stream its output."""
    log("$ " + " ".join(args))
    return subprocess.run([sys.executable, *args], cwd=HERE).returncode


def store_max_period() -> int | None:
    """Latest YYYYMM present in EVERY flow, or None if empty/unreadable.

    Taking the minimum across flows keeps the --retry loop alive when StatCan
    posts the bulk files at different times on release day (e.g. July 2026:
    imports and domestic exports had the new month at 08:35 but total exports
    was still the prior month's file)."""
    if not DETAIL.exists():
        return None
    try:
        import duckdb
        return duckdb.connect().execute(
            f"SELECT min(m) FROM (SELECT max(year*100+month) AS m FROM "
            f"read_parquet('{DETAIL.as_posix()}/**/*.parquet', "
            f"hive_partitioning=true) GROUP BY flow)").fetchone()[0]
    except Exception as e:
        log(f"(could not read store max period: {e})")
        return None


def load_r2_env() -> dict:
    """R2 creds from ~/cimt_r2.env(.rtf) as a dict, or {} if none found."""
    for p in R2_ENV_FILES:
        if not p.exists():
            continue
        if p.suffix == ".rtf":
            text = subprocess.run(["textutil", "-convert", "txt", "-stdout",
                                   str(p)], capture_output=True, text=True).stdout
        else:
            text = p.read_text()
        env = {}
        for line in text.splitlines():
            m = re.match(r"\s*(R2_[A-Z_]+)\s*=\s*(\S+)\s*$", line)
            if m:
                env[m.group(1)] = m.group(2)
        if env:
            return env
    return {}


def publish_online(r2: dict) -> None:
    """Rebuild + upload the trimmed online slice to R2 (best-effort)."""
    log("publishing trimmed slice to R2…")
    rc = subprocess.run([sys.executable, "publish.py", "--stage", "--upload"],
                        cwd=HERE, env={**os.environ, **r2}).returncode
    log("R2 publish done" if rc == 0 else f"R2 publish failed (exit {rc})")


def main() -> int:
    ap = argparse.ArgumentParser(description="CIMT monthly refresh")
    cur = dt.date.today().year
    ap.add_argument("--years", default=f"{cur-1},{cur}",
                    help="years to refresh (default: previous + current)")
    ap.add_argument("--retry", action="store_true",
                    help="on a release day, re-check until a new month lands "
                         f"({RETRY_TRIES}x, {RETRY_INTERVAL//60} min apart)")
    ap.add_argument("--no-publish", action="store_true",
                    help="skip updating the online R2 slice")
    args = ap.parse_args()

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        age_h = (time.time() - LOCK.stat().st_mtime) / 3600
        if age_h < 6:                      # a real run rarely exceeds minutes
            log(f"another refresh started {age_h:.1f}h ago (lock present); exiting")
            return 0
        log("stale lock found (>6h); overriding")
    LOCK.write_text(str(dt.datetime.now()))

    t0 = time.time()
    before = store_max_period()
    log(f"=== CIMT refresh start (years {args.years}, latest in store "
        f"{before}) ===")
    tries = RETRY_TRIES if args.retry else 1
    try:
        for attempt in range(1, tries + 1):
            # --force-download: the on-disk current-year zip is last month's
            # copy; re-pull to pick up the new month + back-revisions.
            rc = run(["ingest.py", "--years", args.years,
                      "--flows", "imp,exp_tot,exp_dom", "--force-download"])
            if rc != 0:
                log(f"ingest failed (exit {rc}); skipping dimensions")
                return rc
            after = store_max_period()
            if before is None or (after is not None and after > before):
                log(f"new data: latest period {before} -> {after}")
                break
            log(f"no new month yet (latest still {after}); "
                f"attempt {attempt}/{tries}")
            if attempt < tries:
                log(f"sleeping {RETRY_INTERVAL//60} min for the release to post…")
                time.sleep(RETRY_INTERVAL)
        rc = run(["dimensions.py"])        # rebuild labels from newest year
        if rc != 0:
            log(f"dimensions failed (exit {rc})")
            return rc
        # Keep the online R2 slice current (best-effort; local store is primary).
        if args.no_publish:
            log("--no-publish: skipping online R2 update")
        else:
            r2 = load_r2_env()
            if r2:
                publish_online(r2)
            else:
                log(f"no R2 creds ({' or '.join(str(p) for p in R2_ENV_FILES)}) "
                    f"— skipping online publish")
    finally:
        LOCK.unlink(missing_ok=True)
    log(f"=== CIMT refresh done in {time.time()-t0:.0f}s ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
