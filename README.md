# Canada Trade Explorer

A Canadian merchandise-trade explorer (USA-Trade-Online style) over the StatCan
**CIMT** bulk annual CSVs, using a local **DuckDB + Hive-partitioned Parquet**
store so aggregations are sub-second — no per-vector WDS round-trips. (Internal
dir/code name remains `cimt`.)

Status: **all five phases are built, and the app is deployed.** Ingest pipeline
+ rollups + dimension lookups (Phase 1), the Flask query API on port 5003
(Phase 2), the query-builder UI `cimt-explorer.html` served at `/` (Phase 3),
the release-date monthly refresh (Phase 4), and the Cloudflare R2 publish behind
the public `/trade` page (Phase 5). See
[How this is deployed](#how-this-is-deployed) for the live wiring.

Running it needs a data store, which is **not** in the repo (`data/` is
git-ignored). If yours is empty or holds only the 2007/2012/2017/2023 sample,
run the full backfill first: `python ingest.py --years 2007-2026`.

Open it: `python api.py` then visit http://127.0.0.1:5003/

## Phase 0 findings (verified 2026-06-26)

Bulk source — one zip per flow per year, **same path for every year 2007–present**:

```
https://www150.statcan.gc.ca/n1/pub/71-607-x/2021004/zip/CIMT-CICM_<FLOW>_<YEAR>.zip
  <FLOW> = Imp | Tot_Exp | Dom_Exp
```

Each zip contains:
- one **HS-detail** CSV — imports = **HS10**, exports = **HS8**
- StatCan's own HS6 / HS2 rollup CSVs (we use these only to cross-check ours)
- fixed-width dimension lookup `.TXT` files

Key facts that shaped the pipeline:
- **Data CSVs are UTF-8** (CRLF). **Dimension `.TXT` files are Latin-1.**
- **Schema asymmetry:** imports carry a `Province` column, **exports do not**
  (both carry `State`). Columns are mapped by header name, not position.
- Columns: `YearMonth, HS, Country, [Province,] State, Value[, Quantity, UoM]`.
- **Quantity is not always summable:** ~2.6% of HS6 codes mix units of measure
  among their children. Rollups sum quantity only when the UoM is consistent,
  otherwise quantity/uom are nulled.
- Our HS10→HS6 / HS8→HS6 rollups **reproduce StatCan's shipped rollups exactly**
  (row count + total value). 2023 totals: imports $757.1B, exports $767.9B.
- **No HS4 description file is shipped** (only HS2/HS6/HS8/HS10). `dimensions.py`
  fills this by pulling the ~1,229 standard WCO HS4 headings from a public-domain
  dataset, so the product tree is labelled at every level.
- Footprint: 2023 imports+exports ≈ 180 MB Parquet. Full 2007–present, 3 flows
  ≈ 4–5 GB (within the plan's 5–12 GB estimate).

## Layout

```
cimt/
  ingest.py        download zips → detail Parquet + HS8/6/4/2 rollups
  dimensions.py    parse Latin-1 fixed-width lookups → dim Parquet
  data/            (git-ignored)
    raw/           downloaded zips
    parquet/
      detail/flow=<imp|exp_tot|exp_dom>/year=<yyyy>/data.parquet   HS10/HS8
      hs8|hs6|hs4|hs2/flow=…/year=…/data.parquet                   rollups
      dim/dim_hs|dim_country|dim_province|dim_state|dim_uom.parquet
```

Unified fact schema (every flow/tier): `month, hs, country, province, state,
value, quantity, uom` + partitions `flow, year`. `province` is null for exports.

## Usage

```bash
pip install duckdb pyarrow requests          # one-time

# Ingest (downloads zips if absent; idempotent per flow-year):
python ingest.py --years 2023                       # one year, all 3 flows
python ingest.py --years 2007-2026                  # full backfill
python ingest.py --years 2023 --flows imp,exp_tot   # subset of flows
python ingest.py --years 2023 --no-download         # use cached zips

# Dimension lookups (codes/labels update yearly — use the newest year):
python dimensions.py                                # newest zip in data/raw
python dimensions.py --year 2025
```

### Query example (DuckDB)

```python
import duckdb
con = duckdb.connect()
P = "data/parquet"
con.execute(f"""
  SELECT c.en partner, sum(f.value) v
  FROM read_parquet('{P}/hs2/**/*.parquet', hive_partitioning=true) f
  JOIN read_parquet('{P}/dim/dim_country.parquet') c
    ON f.country=c.code AND c.valid_to='999912'
  WHERE f.flow='imp' AND f.year=2023 AND f.hs='87'   -- vehicles
  GROUP BY 1 ORDER BY v DESC LIMIT 5
""").fetchall()    # → US $73.8B, MX $17.5B, JP $8.9B, KR $7.0B, DE $6.0B; ~5 ms
```

## Query API (Phase 2)

```bash
pip install -r requirements.txt
python api.py            # http://127.0.0.1:5003  (CIMT_HOST/PORT/CIMT_DATA env-configurable)
```

| Endpoint | Purpose |
|----------|---------|
| `GET  /api/health` | store status + flow-years present |
| `GET  /api/dimensions` | dropdown data: countries, provinces, states, UoM, HS2 chapters |
| `GET  /api/hs/children?flow&code` | next HS level under a parent code (tree drill-down) |
| `POST /api/query` | aggregated pivot query (JSON in/out) |
| `POST /api/export` | same query as an `.xlsx` download |

`/api/query` body (all filters optional except `flow`):

```jsonc
{
  "flow": "imp",                              // imp | exp_tot | exp_dom
  "hs_level": 2,                              // tier to read: 2/4/6/8, else detail
  "filters": {
    "hs": ["87"],                            // HS code prefixes (any-of)
    "country": ["US","MX"], "province": ["ON"],
    "year": {"from": 2017, "to": 2023},      // or [2017,2023]; also "month": [1..12]
  },
  "group_by": ["year","country"],            // any of year/month/hs/country/province/state/uom
  "measure": "value",                        // value | quantity (qty nulled when UoM mixes)
  "limit": 1000
}
```

The API runs on the rollup tiers, so typical queries return in a few ms (the
first query after start-up is ~200 ms while DuckDB warms the Parquet cache).
`/api/query` also accepts `filters.period = {from:"YYYYMM", to:"YYYYMM"}` for
month-precise ranges, and sorts time-series breakdowns chronologically.

## Query-builder UI (Phase 3)

`cimt-explorer.html` (served at `/`) — single-file vanilla-JS app, Chart.js, and
Globe and Mail brand styling (`--brand` red `#e2001a`, Pratt-Bold display serif)
over Fluent-neutral greys. Pick a flow — imports, total/domestic exports,
or **balance** (total exports − imports, value-only, ranked by magnitude);
optionally filter by product (HS tree drill-down), partner country, province,
**U.S. state** (local API mode only — the online slice has no state column),
and a month-precise time range. "Break down by" country / province / U.S. state
/ HS chapter-heading-subheading / over time / **top-N partners over time**
(ranks partners over the selection, then one line each); rankings show an
adjustable top N (5/10/20/50). Over-time views come in four flavours that all
avoid the partial-current-year cliff: **annual** (complete calendar years only,
with a note when the current year is excluded), **year-to-date** (every year
truncated to the latest available month, so the current year compares fairly),
**monthly**, and **12-month sum** (trailing rolling total). For over-time views,
show level or period-over-period / year-over-year % change. Multi-selections plot Combined
(sum) or Separate; series run in parallel and every chart is legend-labelled
with what it plots. Non-blocking warnings flag overlapping HS prefixes in a
Combined sum (double-counting) and mixed units-of-measure in quantity mode;
stale results dim until re-run. The whole query lives in a shareable `?q=` URL
(a shared link re-opens straight onto its chart). Results render as a chart
(+ `.png` download) and share table with an `.xlsx` export that includes a
metadata sheet (source, filters, retrieval date, link). Queries are national
by default (province filter optional).

## Monthly refresh (Phase 4)

StatCan trade releases lag ~6 weeks. The current-year zip holds all YTD months
and is the only **mutable** partition; prior years can still see small
back-revisions. `refresh.py` re-downloads (forced) the current + previous year,
re-ingests them (idempotent partition overwrite), and rebuilds the dimension
labels. A lock file guards against overlapping runs.

```bash
python refresh.py                  # current + previous year
python refresh.py --years 2026     # one year
```

**Trigger (macOS launchd) — fires on the actual release dates.** StatCan
publishes the merchandise-trade release calendar; those dates live in
`release_dates.txt`, and `make_refresh_plist.py` turns them into the launchd
agent — one trigger per release day at **08:35 local (ET)**, ~5 min after the
08:30 release. With `--retry`, the job re-checks every 15 min for ~1h45m if the
bulk file posts late, so the app is current the morning you need it.

The job runs from the TCC-safe `~/statcan-explorer` clone (background jobs can't
read `~/Desktop`), like the EV detector and the other StatCan jobs.

```bash
# 1. ensure cimt/ and its data/ exist under ~/statcan-explorer
#    (git pull there; seed data by copying ~/Desktop/StatCanApp/cimt/data over,
#     or run `python ingest.py --years 2007-2026` once from that clone)
# 2. (re)generate the plist from the date list, then install + load it
python ~/statcan-explorer/cimt/make_refresh_plist.py
cp ~/statcan-explorer/cimt/com.statcan.cimt-refresh.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.statcan.cimt-refresh.plist
launchctl enable gui/$(id -u)/com.statcan.cimt-refresh

# verify / run once now / inspect logs
launchctl list | grep cimt
launchctl kickstart -k gui/$(id -u)/com.statcan.cimt-refresh
tail -f ~/statcan-explorer/cimt/cimt_refresh.log
```

The current date list runs through the **Feb 2027** release (December 2026
data). When StatCan publishes the next schedule, append the dates to
`release_dates.txt`, re-run `make_refresh_plist.py`, and re-`bootout`/`bootstrap`
the agent. launchd uses the Mac's **local** time, so these assume the machine is
on Eastern; change `RELEASE_HOUR/MIN` in `make_refresh_plist.py` otherwise. If
the Mac is asleep at 08:35, launchd runs the job on wake.

## Online deployment (Phase 5)

The UI is **dual-mode** and picks its data source automatically:
- served by a local `api.py` (full store on disk) → **API mode**, full 20-yr/HS10;
- served as a static page with no local API → **WASM mode**: DuckDB-WASM runs in
  the browser and queries a **trimmed slice in Cloudflare R2** (last 10 yr, HS6).

So coworkers get the online slice with **zero server compute** (R2 just serves
files; the browser does the querying), while you keep full detail locally.
`?wasm=1` forces WASM mode for testing; `?r2=<base>` overrides the slice URL.

### 1. Build the slice

```bash
python publish.py --stage          # builds data/publish/parquet (~1 GB) + manifest.json
```

Test it locally before R2 exists — `api.py` serves the staged slice at `/r2`
with CORS+range, so open `http://127.0.0.1:5003/?wasm=1`.

### 2. Cloudflare R2 (one-time)

1. Create a bucket (e.g. `cimt-trade`) and an **R2 API token** (Object Read &
   Write). Note the account ID + access key/secret.
2. Enable **public access** — either the managed `https://<hash>.r2.dev` URL or
   a custom domain. That public base URL is your `R2_BASE`.
3. Add a **CORS policy** so browsers can range-read the parquet (the live
   origin needs `GET`, the `Range` request header, and the range response
   headers exposed):

   ```json
   [{ "AllowedOrigins": ["https://statcan.jasonkirby.ca"],
      "AllowedMethods": ["GET", "HEAD"],
      "AllowedHeaders": ["Range"],
      "ExposeHeaders": ["Content-Range", "Accept-Ranges", "Content-Length"] }]
   ```

### 3. Upload

```bash
pip install boto3
export R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=cimt-trade
python publish.py --upload         # syncs the slice + manifest under the "cimt" prefix
```

The monthly refresh keeps the online slice current: after `refresh.py`, re-run
`python publish.py --stage --upload` (a few MB change). Add that line to the
launchd job once R2 is set up.

### 4. Deploy the page

`proxy.py` — which lives in the **`jmkyyz/statcan-explorer`** monorepo, not in
this repo — serves the UI at **<https://statcan.jasonkirby.ca/trade>** and injects
the R2 URL from an env var (so it's not hard-coded). On Render, set:

```
CIMT_R2_BASE = https://<your-r2-public-base>/cimt
```

then push to `main` (Render auto-deploys). No new server dependencies — `/trade`
is a static page; all querying happens in the visitor's browser. The old
`/api/cimt*` WDS routes are untouched.

## How this is deployed

The public page is **<https://statcan.jasonkirby.ca/trade>**. Nothing in this
repo serves it directly — the request path is:

```
statcan.jasonkirby.ca              custom domain, proxied through Cloudflare
        ↓                          (Cloudflare injects its Web Analytics beacon
        ↓                           at the edge — no code in either repo does)
Render web service "statcan-explorer"
        ↓                          render.yaml → startCommand: python proxy.py
proxy.py   @app.route("/trade")
        ↓                          reads cimt/cimt-explorer.html, injects
        ↓                          <script>window.R2_BASE="…"</script> into <head>
visitor's browser
                                   DuckDB-WASM queries the Parquet slice in
                                   Cloudflare R2 over HTTP range requests
```

Render runs **no query code**: it reads one HTML file and injects one string.
Every aggregation happens in the reader's browser. That is why the online copy
costs nothing to serve, and why porting it elsewhere needs only static hosting
plus an object store — no backend.

### Where each piece lives

| Piece | Location |
|-------|----------|
| Page served at `/trade` | `cimt/cimt-explorer.html` in **`jmkyyz/statcan-explorer`** — a copy of this repo's file |
| The `/trade` route | `proxy.py` in **`jmkyyz/statcan-explorer`** (not in this repo) |
| Render service definition | `render.yaml` in **`jmkyyz/statcan-explorer`** |
| `CIMT_R2_BASE` env var | Render dashboard only — in no repo |
| Data slice | Cloudflare R2, public base `https://pub-a740e33eeabf4a1f87594232306f24e2.r2.dev/cimt` |
| Monthly refresh | launchd on a home Mac (`com.statcan.cimt-refresh.plist`) |

**This repo is a split-off copy.** Production is served from the monorepo's
`cimt/` directory, so a change here does not reach the live site until it is
mirrored there — and vice versa. The two were byte-identical at the time of the
split. Decide which copy is canonical before editing both.

### Recovering the R2 base URL

`CIMT_R2_BASE` is set by hand in the Render dashboard, so it exists in no repo.
Quickest way to read it back: `view-source:` the live page and look for
`window.R2_BASE=` in the `<head>`, where `proxy.py` injects it. Otherwise Render
→ service → Environment, or Cloudflare → R2 → bucket → Settings.

### Operational caveats

- **Live data is refreshed from a home Mac.** `refresh.py` runs under launchd
  on StatCan release mornings and pushes the rebuilt slice to R2. If that Mac is
  off or the job fails, the site silently serves stale data — there is no
  server-side refresh and no alerting.
- **`release_dates.txt` runs out after 2027-02-04.** After that the launchd
  triggers stop firing until the next StatCan schedule is appended and
  `make_refresh_plist.py` is re-run.
- **`make_refresh_plist.py` hardcodes `/Users/jasonkirby/statcan-explorer/cimt/`**
  and is macOS-only. Any other host needs cron, a systemd timer, or scheduled CI.
- **Three third-party CDNs are load-bearing:** Chart.js and SheetJS from cdnjs,
  DuckDB-WASM from jsDelivr. A host with a strict CSP must self-host all three.
