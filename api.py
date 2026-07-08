#!/usr/bin/env python3
"""CIMT Trade Explorer — query API (Phase 2).

A small Flask service that answers trade queries against the local DuckDB +
Parquet store built by ingest.py / dimensions.py. Aggregations are sub-second
because the data is pre-organized and pre-rolled-up — no per-vector WDS calls.

Endpoints:
    GET  /api/health                  store status + flow-years present
    GET  /api/dimensions              dropdown data: countries, provinces,
                                      states, UoM, HS2 chapters
    GET  /api/hs/children?flow&code   next HS level under a parent code (tree)
    POST /api/query                   aggregated pivot query (JSON in/out)
    POST /api/export                  same query as an .xlsx download

Config via env vars (so the same app runs locally or on a shared server):
    CIMT_DATA   path to the parquet store   (default: ./data/parquet)
    CIMT_HOST   bind host                   (default: 127.0.0.1)
    PORT        bind port                   (default: 5003; 5002 is the lobbyist app)

Usage:
    pip install flask flask-cors duckdb pyarrow openpyxl
    python api.py
"""
from __future__ import annotations

import io
import os
import time
from pathlib import Path

import duckdb
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

HERE = Path(__file__).resolve().parent
PARQUET = Path(os.environ.get("CIMT_DATA", HERE / "data" / "parquet"))
DIM = PARQUET / "dim"

FLOWS = {"imp", "exp_tot", "exp_dom"}
# HS classification used for labels: imports = M, exports = X (HS2 = both).
FLOW_CLASS = {"imp": "M", "exp_tot": "X", "exp_dom": "X"}
# Dimensions a caller may group by / filter on (whitelist — guards the SQL).
DIMS = {"year", "month", "hs", "country", "province", "state", "uom"}
TIERS = {2: "hs2", 4: "hs4", 6: "hs6", 8: "hs8"}  # else -> detail

app = Flask(__name__)
CORS(app)

# One parent DuckDB connection holds the catalog (label views); every request
# runs on its own con.cursor() because Flask serves requests on threads and
# the UI issues parallel queries — DuckDB connections aren't thread-safe to share.
con = duckdb.connect(database=":memory:")


# --- helpers ---------------------------------------------------------------
def _q(path: Path) -> str:
    """A read_parquet(...) source over a Hive-partitioned dataset."""
    return (f"read_parquet('{path.as_posix()}/**/*.parquet', "
            f"hive_partitioning=true)")


def tier_source(flow: str, level: int | None) -> str:
    """Parquet source for the smallest tier that can answer `level`.

    Exports have no hs8/detail-above-8 tier (their detail *is* HS8); imports
    detail is HS10. Falls back to the detail dataset when a tier is absent.
    """
    name = TIERS.get(level or 0, "detail")
    if not (PARQUET / name / f"flow={flow}").exists():
        name = "detail"
    return _q(PARQUET / name)


def label_join(con):
    """Register HS/country/etc. label lookups if their parquet exists."""
    for name in ("dim_hs", "dim_country", "dim_province", "dim_state", "dim_uom"):
        f = DIM / f"{name}.parquet"
        if f.exists():
            con.execute(f"CREATE OR REPLACE VIEW {name} AS "
                        f"SELECT * FROM read_parquet('{f.as_posix()}')")


label_join(con)


# --- routes ----------------------------------------------------------------
@app.route("/")
def index():
    # Serve the Phase 3 UI if present, else a friendly note.
    if (HERE / "cimt-explorer.html").exists():
        return send_from_directory(HERE, "cimt-explorer.html")
    return ("CIMT API running. UI (cimt-explorer.html) not built yet. "
            "Try /api/health."), 200


@app.route("/r2/<path:p>")
def r2_local(p):
    """Serve the staged publish/ slice locally (CORS + HTTP range) so the
    browser-WASM path can be tested before R2 exists. Production serves these
    files from Cloudflare R2 instead; this route is a dev/self-host convenience."""
    base = HERE / "data" / "publish" / "parquet"
    resp = send_from_directory(base, p, conditional=True)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = \
        "Content-Range, Accept-Ranges, Content-Length"
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


@app.route("/api/health")
def health():
    if not PARQUET.exists():
        return jsonify(ok=False, error=f"no store at {PARQUET}"), 503
    det = _q(PARQUET / "detail")
    c = con.cursor()
    rows = c.execute(
        f"SELECT flow, year, count(*) n FROM {det} "
        f"GROUP BY 1,2 ORDER BY 2,1").fetchall()
    pmin, pmax = c.execute(
        f"SELECT min(year*100+month), max(year*100+month) FROM {det}").fetchone()
    return jsonify(ok=True, data_dir=str(PARQUET),
                   period_min=pmin, period_max=pmax,
                   flow_years=[{"flow": r[0], "year": r[1], "rows": r[2]}
                               for r in rows])


@app.route("/api/dimensions")
def dimensions():
    """Small lookup lists to populate UI dropdowns."""
    c = con.cursor()
    def rows(view, extra=""):
        if not (DIM / f"{view}.parquet").exists():
            return []
        return [dict(zip(("code", "en"), r)) for r in c.execute(
            f"SELECT DISTINCT code, en FROM {view} "
            f"WHERE valid_to='999912' {extra} ORDER BY code").fetchall()]
    hs2 = [dict(zip(("code", "en"), r)) for r in c.execute(
        "SELECT DISTINCT hs, en FROM dim_hs WHERE hs_len=2 AND valid_to='999912' "
        "ORDER BY hs").fetchall()] if (DIM / "dim_hs.parquet").exists() else []
    return jsonify(
        flows=[{"code": "imp", "en": "Imports"},
               {"code": "exp_tot", "en": "Exports (total)"},
               {"code": "exp_dom", "en": "Exports (domestic)"}],
        hs2=hs2,
        countries=rows("dim_country"),
        provinces=rows("dim_province"),
        states=rows("dim_state"),
        uom=rows("dim_uom"),
    )


@app.route("/api/hs/children")
def hs_children():
    """Next HS level beneath `code` for the tree UI.

    code='' -> HS2 chapters; len 2 -> HS4; 4 -> HS6; 6 -> HS8/HS10 detail.
    Labels use the flow's classification (M for imports, X for exports).
    """
    flow = request.args.get("flow", "imp")
    code = request.args.get("code", "").strip()
    cls = FLOW_CLASS.get(flow, "M")
    child_len = {0: 2, 2: 4, 4: 6, 6: (10 if flow == "imp" else 8)}.get(len(code))
    if child_len is None:
        return jsonify(error="leaf node"), 400
    # HS2 + HS4 labels are flow-agnostic ('both'); HS6/8/10 use the flow class.
    where_cls = "classification='both'" if child_len in (2, 4) else \
                f"classification='{cls}'"
    src = tier_source(flow, child_len if child_len in TIERS else None)
    sql = f"""
        WITH present AS (
            SELECT DISTINCT substr(hs,1,{child_len}) hs
            FROM {src} WHERE flow=? {"AND hs LIKE ?" if code else ""}
        )
        SELECT p.hs, d.en
        FROM present p
        LEFT JOIN (SELECT hs, any_value(en) en FROM dim_hs
                   WHERE hs_len={child_len} AND {where_cls} GROUP BY hs) d
          ON p.hs = d.hs
        ORDER BY p.hs"""
    params = [flow] + ([code + "%"] if code else [])
    out = [{"code": r[0], "en": r[1]}
           for r in con.cursor().execute(sql, params).fetchall()]
    return jsonify(parent=code, children=out)


@app.route("/api/hs/search")
def hs_search():
    """Search HS codes by description text OR code, across all levels.

    e.g. "gold" -> HS4 7108 …; "7108" -> 7108 and its sub-codes; "73" -> HS2 73
    then its headings. Restricted to the flow's classification (+ shared HS2/HS4).
    Code-prefix matches rank first, then shorter (broader) codes.
    """
    flow = request.args.get("flow", "imp")
    q = request.args.get("q", "").strip()
    if len(q) < 2 or not (DIM / "dim_hs.parquet").exists():
        return jsonify(results=[])
    cls = FLOW_CLASS.get(flow, "M")
    ql = q.lower()
    # Rank: code-prefix first, then label word-start (e.g. "gold" -> "Gold…"
    # ahead of "mangolds"), then any substring; broader (shorter) codes first.
    rows = con.cursor().execute("""
        SELECT hs, any_value(en) en, any_value(hs_len) hs_len
        FROM dim_hs
        WHERE valid_to='999912' AND (classification='both' OR classification=?)
          AND (hs LIKE ? OR lower(en) LIKE ?)
        GROUP BY hs
        ORDER BY (hs LIKE ?) DESC,
                 (lower(any_value(en)) LIKE ?) DESC,
                 (lower(any_value(en)) LIKE ?) DESC,
                 length(hs), hs
        LIMIT 100
    """, [cls, q + "%", "%" + ql + "%",
          q + "%", ql + "%", "% " + ql + "%"]).fetchall()
    return jsonify(results=[{"code": r[0], "en": r[1], "hs_len": r[2]}
                            for r in rows])


def _build_query(body: dict):
    """Validate a query body and return (sql, params, group_cols)."""
    flow = body.get("flow")
    if flow not in FLOWS:
        raise ValueError(f"flow must be one of {sorted(FLOWS)}")
    measure = body.get("measure", "value")
    if measure not in ("value", "quantity"):
        raise ValueError("measure must be 'value' or 'quantity'")
    group_by = body.get("group_by", [])
    if not isinstance(group_by, list) or any(g not in DIMS for g in group_by):
        raise ValueError(f"group_by items must be within {sorted(DIMS)}")

    level = body.get("hs_level")
    src = tier_source(flow, level)

    where, params = ["flow = ?"], [flow]
    f = body.get("filters", {}) or {}
    # HS prefix filter (any of the given codes)
    if f.get("hs"):
        codes = f["hs"] if isinstance(f["hs"], list) else [f["hs"]]
        where.append("(" + " OR ".join("hs LIKE ?" for _ in codes) + ")")
        params += [c + "%" for c in codes]
    for dim in ("country", "province", "state", "uom"):
        if f.get(dim):
            vals = f[dim] if isinstance(f[dim], list) else [f[dim]]
            where.append(f"{dim} IN (" + ",".join("?" for _ in vals) + ")")
            params += vals
    # Period range (YYYYMM..YYYYMM) — preferred; also constrains year for
    # partition pruning. Falls back to plain year/month filters below.
    per = f.get("period")
    if isinstance(per, dict) and (per.get("from") or per.get("to")):
        if per.get("from"):
            pf = int(per["from"])
            where.append("(year*100+month) >= ?"); params.append(pf)
            where.append("year >= ?"); params.append(pf // 100)
        if per.get("to"):
            pt = int(per["to"])
            where.append("(year*100+month) <= ?"); params.append(pt)
            where.append("year <= ?"); params.append(pt // 100)
    else:
        yr = f.get("year")
        if isinstance(yr, dict):
            if yr.get("from"):
                where.append("year >= ?"); params.append(int(yr["from"]))
            if yr.get("to"):
                where.append("year <= ?"); params.append(int(yr["to"]))
        elif isinstance(yr, list) and yr:
            where.append("year IN (" + ",".join("?" for _ in yr) + ")")
            params += [int(v) for v in yr]
        if f.get("month"):
            ms = f["month"] if isinstance(f["month"], list) else [f["month"]]
            where.append("month IN (" + ",".join("?" for _ in ms) + ")")
            params += [int(v) for v in ms]

    sel = list(group_by)
    # Quantity is only meaningful where UoM is consistent within the group.
    if measure == "quantity":
        agg = ("CASE WHEN count(DISTINCT uom)=1 THEN sum(quantity) END AS "
               "quantity, any_value(uom) AS uom")
    else:
        agg = "sum(value) AS value"
    select_cols = (", ".join(sel) + ", " if sel else "") + agg
    group_clause = f"GROUP BY {', '.join(sel)}" if sel else ""
    # Time-series breakdowns sort chronologically; rankings sort by measure.
    temporal = [g for g in sel if g in ("year", "month")]
    order = (", ".join(temporal) + " ASC") if temporal else f"{measure} DESC"
    limit = int(body.get("limit", 1000))
    sql = (f"SELECT {select_cols} FROM {src} WHERE {' AND '.join(where)} "
           f"{group_clause} ORDER BY {order} LIMIT {limit}")
    return sql, params, sel


LABEL_VIEWS = {"country": "dim_country", "province": "dim_province",
               "state": "dim_state", "uom": "dim_uom"}


def attach_labels(rows: list[dict], cols: list[str], flow: str, cur) -> None:
    """Add a `<dim>_label` to each row for grouped code dimensions, in place."""
    for dim in cols:
        view = LABEL_VIEWS.get(dim)
        if view and (DIM / f"{view}.parquet").exists():
            m = dict(cur.execute(
                f"SELECT code, en FROM {view} WHERE valid_to='999912'").fetchall())
            for r in rows:
                r[f"{dim}_label"] = m.get(r[dim])
    if "hs" in cols and (DIM / "dim_hs.parquet").exists():
        cls = FLOW_CLASS.get(flow, "M")
        m = dict(cur.execute(
            "SELECT hs, any_value(en) FROM dim_hs WHERE valid_to='999912' "
            "AND (classification='both' OR classification=?) GROUP BY hs",
            [cls]).fetchall())
        for r in rows:
            r["hs_label"] = m.get(r["hs"])


@app.route("/api/query", methods=["POST"])
def query():
    body = request.get_json(force=True) or {}
    try:
        sql, params, group_cols = _build_query(body)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    t = time.time()
    c = con.cursor()
    rel = c.execute(sql, params)
    cols = [col[0] for col in rel.description]
    rows = [dict(zip(cols, r)) for r in rel.fetchall()]
    attach_labels(rows, group_cols, body.get("flow"), c)
    return jsonify(columns=cols, rows=rows, row_count=len(rows),
                   elapsed_ms=round((time.time() - t) * 1000, 1), sql=sql)


@app.route("/api/export", methods=["POST"])
def export():
    body = request.get_json(force=True) or {}
    try:
        sql, params, _ = _build_query(body)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    df = con.cursor().execute(sql, params).fetch_arrow_table()
    buf = io.BytesIO()
    # Use DuckDB's own XLSX writer via a temp file would need the excel ext;
    # write a pandas-free .xlsx through pyarrow->openpyxl.
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CIMT query"
    ws.append(df.column_names)
    for row in zip(*[df.column(c).to_pylist() for c in df.column_names]):
        ws.append(list(row))
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="cimt-query.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet")


if __name__ == "__main__":
    host = os.environ.get("CIMT_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5003))
    print(f"CIMT API on http://{host}:{port}  (data: {PARQUET})")
    app.run(host=host, port=port, debug=True)
