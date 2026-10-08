"""SQLite project state.

Tables
  projects     one row per Decoder video; chart config + parsed data as JSON
  transcripts  every transcription is kept (versioned) so cues can be
               re-attached to a new take by aligning old words to new words
  cues         chart actions anchored to a word of a transcript version
  jobs         background work (normalize video, transcribe, render)
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    template      TEXT NOT NULL,
    theme         TEXT NOT NULL DEFAULT 'default',
    config_json   TEXT NOT NULL DEFAULT '{}',
    data_json     TEXT NOT NULL DEFAULT '{}',
    csv_name      TEXT,
    video_json    TEXT,            -- {original, master, duration, width, height}
    layout_json   TEXT NOT NULL DEFAULT '{}',   -- per-project layout tweaks (talent crop)
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transcripts (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    source        TEXT NOT NULL,   -- 'faster-whisper:<model>' | 'import'
    language      TEXT,
    words_json    TEXT NOT NULL,   -- [{i, text, start, end, prob}]
    created_at    REAL NOT NULL,
    UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS cues (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    transcript_id INTEGER NOT NULL REFERENCES transcripts(id),
    word_index    INTEGER NOT NULL,
    anchor_word   TEXT NOT NULL,   -- normalised word text when attached
    offset        REAL NOT NULL DEFAULT 0,   -- seconds relative to word start
    action        TEXT NOT NULL,
    params_json   TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'ok', -- ok | moved | orphaned
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,   -- queued | running | done | error
    progress      REAL NOT NULL DEFAULT 0,
    message       TEXT,
    result_json   TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
"""

_local = threading.local()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return conn


def init():
    connect().executescript(SCHEMA)
    with tx() as c:  # jobs can't survive a server restart
        c.execute("UPDATE jobs SET status='error', message='Interrupted (server restarted)'"
                  " WHERE status IN ('queued','running')")


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def now() -> float:
    return time.time()


def loads(s, default=None):
    return json.loads(s) if s else default


# ---- projects -------------------------------------------------------------

def project_row_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "template": r["template"],
        "theme": r["theme"],
        "config": loads(r["config_json"], {}),
        "data": loads(r["data_json"], {}),
        "csv_name": r["csv_name"],
        "video": loads(r["video_json"]),
        "layout": loads(r["layout_json"], {}),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def get_project(pid: int) -> dict | None:
    r = connect().execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    return project_row_to_dict(r) if r else None


def list_projects() -> list[dict]:
    rows = connect().execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
    return [project_row_to_dict(r) for r in rows]


def create_project(name, template, theme, cfg, data, csv_name) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO projects (name, template, theme, config_json, data_json, csv_name,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, template, theme, json.dumps(cfg), json.dumps(data), csv_name, now(), now()),
        )
        return cur.lastrowid


def update_project(pid: int, **fields):
    cols, vals = [], []
    for k, v in fields.items():
        if k in ("config", "data", "video", "layout"):
            k, v = f"{k}_json", json.dumps(v)
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("updated_at=?")
    vals.append(now())
    with tx() as c:
        c.execute(f"UPDATE projects SET {', '.join(cols)} WHERE id=?", (*vals, pid))


def delete_project(pid: int):
    with tx() as c:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


# ---- transcripts ----------------------------------------------------------

def _transcript(r):
    if not r:
        return None
    return {
        "id": r["id"], "project_id": r["project_id"], "version": r["version"],
        "source": r["source"], "language": r["language"],
        "words": loads(r["words_json"], []), "created_at": r["created_at"],
    }


def add_transcript(pid: int, words: list, source: str, language: str | None) -> dict:
    with tx() as c:
        v = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM transcripts WHERE project_id=?",
                      (pid,)).fetchone()[0]
        cur = c.execute(
            "INSERT INTO transcripts (project_id, version, source, language, words_json, created_at)"
            " VALUES (?,?,?,?,?,?)", (pid, v, source, language, json.dumps(words), now()))
        tid = cur.lastrowid
    return get_transcript(tid)


def get_transcript(tid: int) -> dict | None:
    return _transcript(connect().execute("SELECT * FROM transcripts WHERE id=?", (tid,)).fetchone())


def latest_transcript(pid: int) -> dict | None:
    return _transcript(connect().execute(
        "SELECT * FROM transcripts WHERE project_id=? ORDER BY version DESC LIMIT 1", (pid,)).fetchone())


def update_transcript_words(tid: int, words: list):
    with tx() as c:
        c.execute("UPDATE transcripts SET words_json=? WHERE id=?", (json.dumps(words), tid))


# ---- cues -----------------------------------------------------------------

def _cue(r):
    return {
        "id": r["id"], "transcript_id": r["transcript_id"], "word_index": r["word_index"],
        "anchor_word": r["anchor_word"], "offset": r["offset"], "action": r["action"],
        "params": loads(r["params_json"], {}), "status": r["status"],
    }


def list_cues(pid: int) -> list[dict]:
    rows = connect().execute("SELECT * FROM cues WHERE project_id=? ORDER BY id", (pid,)).fetchall()
    return [_cue(r) for r in rows]


def get_cue(cid: int) -> dict | None:
    r = connect().execute("SELECT * FROM cues WHERE id=?", (cid,)).fetchone()
    return _cue(r) if r else None


def add_cue(pid, transcript_id, word_index, anchor_word, action, params, offset=0.0) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO cues (project_id, transcript_id, word_index, anchor_word, offset, action,"
            " params_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, transcript_id, word_index, anchor_word, offset, action, json.dumps(params), now()))
        return cur.lastrowid


def update_cue(cid: int, **fields):
    cols, vals = [], []
    for k, v in fields.items():
        if k == "params":
            k, v = "params_json", json.dumps(v)
        cols.append(f"{k}=?")
        vals.append(v)
    with tx() as c:
        c.execute(f"UPDATE cues SET {', '.join(cols)} WHERE id=?", (*vals, cid))


def delete_cue(cid: int):
    with tx() as c:
        c.execute("DELETE FROM cues WHERE id=?", (cid,))


# ---- jobs -----------------------------------------------------------------

def _job(r):
    if not r:
        return None
    return {
        "id": r["id"], "project_id": r["project_id"], "kind": r["kind"], "status": r["status"],
        "progress": r["progress"], "message": r["message"], "result": loads(r["result_json"]),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def create_job(pid, kind) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO jobs (project_id, kind, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            (pid, kind, "queued", now(), now()))
        return cur.lastrowid


def update_job(jid, **fields):
    if "result" in fields:
        fields["result_json"] = json.dumps(fields.pop("result"))
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))


def get_job(jid) -> dict | None:
    return _job(connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def latest_job(pid, kind) -> dict | None:
    return _job(connect().execute(
        "SELECT * FROM jobs WHERE project_id=? AND kind=? ORDER BY id DESC LIMIT 1",
        (pid, kind)).fetchone())
