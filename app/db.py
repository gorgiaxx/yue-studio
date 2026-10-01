"""SQLite persistence for songs, transcriptions, and generation attempts.

Schema v2 adds generation_attempts: one Song can own many immutable attempts
(each a separate output dir), never overwriting a baseline. Existing rows are
migrated in place — attempt 1 is synthesized from each song's current state.
"""
import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "studio.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    style TEXT NOT NULL,
    lyrics TEXT NOT NULL,
    abc TEXT,
    cot TEXT NOT NULL DEFAULT 'full',
    seed INTEGER NOT NULL DEFAULT 831001,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    audio_path TEXT,
    abc_generated TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    cfg_scale REAL,
    instrumental INTEGER NOT NULL DEFAULT 0,
    score_source TEXT,
    active_attempt TEXT
);
CREATE TABLE IF NOT EXISTS transcriptions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    abc TEXT,
    key TEXT,
    duration REAL,
    out_dir TEXT,
    created_at REAL NOT NULL,
    finished_at REAL,
    updated_at REAL,
    task TEXT NOT NULL DEFAULT 'melody_full',
    melody_only INTEGER NOT NULL DEFAULT 1,
    warnings TEXT,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS generation_attempts (
    id TEXT PRIMARY KEY,
    song_id TEXT NOT NULL,
    parent_attempt TEXT,
    attempt_no INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'generate',
    title TEXT NOT NULL,
    style TEXT NOT NULL,
    lyrics TEXT NOT NULL,
    cot TEXT NOT NULL,
    seed INTEGER NOT NULL,
    cfg_scale REAL,
    abc_input TEXT,
    abc_generated TEXT,
    output_dir TEXT,
    plan_dir TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    audio_path TEXT,
    audio_seconds REAL,
    truncated_abc INTEGER,
    truncated_semantic INTEGER,
    model TEXT,
    model_revision TEXT,
    vae TEXT,
    vae_revision TEXT,
    runtime_version TEXT,
    config_meta TEXT,
    instrumental INTEGER NOT NULL DEFAULT 0,
    score_source TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_songs_created ON songs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trans_created ON transcriptions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_song ON generation_attempts(song_id, attempt_no);
"""

_NEW_SONG_COLS = {"cfg_scale": "REAL", "instrumental": "INTEGER NOT NULL DEFAULT 0",
                  "score_source": "TEXT", "active_attempt": "TEXT"}
_NEW_TRANS_COLS = {"task": "TEXT NOT NULL DEFAULT 'melody_full'",
                   "melody_only": "INTEGER NOT NULL DEFAULT 1",
                   "warnings": "TEXT", "meta": "TEXT"}


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: dict) -> None:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_columns(conn, "songs", _NEW_SONG_COLS)
    _ensure_columns(conn, "transcriptions", _NEW_TRANS_COLS)
    _migrate_legacy_attempts(conn)
    return conn


def _migrate_legacy_attempts(conn: sqlite3.Connection) -> None:
    """Songs created before attempts exist get one synthesized attempt row each.

    Runs on every connect (cheap: one indexed COUNT per new song row); safe
    against concurrent workers because inserts use INSERT OR IGNORE.
    """
    have = conn.execute("SELECT COUNT(*) FROM generation_attempts").fetchone()[0]
    if have:
        # Top up any songs that still lack an attempt row (e.g. rows inserted
        # between worker connects during migration).
        rows = conn.execute(
            "SELECT s.* FROM songs s WHERE NOT EXISTS "
            "(SELECT 1 FROM generation_attempts a WHERE a.song_id = s.id)"
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM songs").fetchall()
    for s in rows:
        # Legacy layout outputs/song-song-{id}; attempt dirs keep their files.
        out_dir = None
        if s["audio_path"]:
            p = Path(s["audio_path"])
            if p.is_file():
                out_dir = str(p.parent)
        conn.execute(
            "INSERT OR IGNORE INTO generation_attempts "
            "(id,song_id,attempt_no,kind,title,style,lyrics,cot,seed,abc_input,"
            "abc_generated,output_dir,status,audio_path,created_at,finished_at,instrumental)"
            " VALUES (?,?,1,'generate',?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (f"{s['id']}-a1", s["id"], s["title"], s["style"], s["lyrics"],
             s["cot"], s["seed"], s["abc"], s["abc_generated"], out_dir,
             "done" if s["audio_path"] else ("failed" if s["error"] else "pending"),
             s["audio_path"], s["created_at"], s["finished_at"]))
        if not s["active_attempt"]:
            conn.execute("UPDATE songs SET active_attempt=? WHERE id=?",
                         (f"{s['id']}-a1", s["id"]))
    if rows:
        conn.commit()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ---- songs ----

def create_song(title, style, lyrics, abc, cot, seed, cfg_scale=None,
                instrumental=False, score_source=None) -> dict:
    sid = new_id("song")
    t = now()
    with get_db() as db:
        db.execute(
            "INSERT INTO songs (id,title,style,lyrics,abc,cot,seed,status,"
            "created_at,updated_at,cfg_scale,instrumental,score_source)"
            " VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?,?)",
            (sid, title, style, lyrics, abc, cot, seed, t, t,
             cfg_scale, int(bool(instrumental)), score_source))
    create_attempt(song_id=sid, kind="generate", title=title, style=style,
                   lyrics=lyrics, cot=cot, seed=seed, cfg_scale=cfg_scale,
                   abc_input=abc, instrumental=instrumental, score_source=score_source)
    return get_song(sid)


def get_song(sid: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM songs WHERE id=?", (sid,)).fetchone()
        return row_to_dict(row) if row else None


def list_songs(limit: int = 100) -> list[dict]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM songs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [row_to_dict(r) for r in rows]


def update_song(sid: str, **fields) -> None:
    if not fields:
        return
    fields = dict(fields, updated_at=now())
    sets = ", ".join(f"{k}=?" for k in fields)
    with get_db() as db:
        db.execute(f"UPDATE songs SET {sets} WHERE id=?", (*fields.values(), sid))


def delete_song(sid: str, outputs_dir: Path) -> None:
    song = get_song(sid)
    if not song:
        return
    # Remove every attempt's output dir (v2 nested + v1 flat layouts).
    for d in {outputs_dir / sid, outputs_dir / f"song-{sid}"}:
        if d.is_dir():
            import shutil
            shutil.rmtree(d, ignore_errors=True)
    with get_db() as db:
        db.execute("DELETE FROM generation_attempts WHERE song_id=?", (sid,))
        db.execute("DELETE FROM songs WHERE id=?", (sid,))
        db.commit()


def create_attempt(song_id, kind, title, style, lyrics, cot, seed,
                   cfg_scale=None, abc_input=None, parent_attempt=None,
                   instrumental=False, score_source=None) -> dict:
    """Append a new attempt. Does NOT flip active_attempt — that happens on
    completion (_finish_activate), so chained rerun/variant/regenerate calls
    keep resolving their base against the currently-viewed attempt."""
    with get_db() as db:
        row = db.execute("SELECT COALESCE(MAX(attempt_no),0)+1 AS n FROM"
                         " generation_attempts WHERE song_id=?", (song_id,)).fetchone()
        aid = f"{song_id}-a{row['n']}"
        t = now()
        db.execute(
            "INSERT INTO generation_attempts (id,song_id,parent_attempt,attempt_no,"
            "kind,title,style,lyrics,cot,seed,cfg_scale,abc_input,instrumental,"
            "score_source,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
            (aid, song_id, parent_attempt, row["n"], kind, title, style, lyrics,
             cot, seed, cfg_scale, abc_input, int(bool(instrumental)),
             score_source, t))
        # Song shows as queued; the VIEWED attempt stays the active one until
        # the new attempt finishes.
        db.execute("UPDATE songs SET status='pending', error=NULL WHERE id=?", (song_id,))
    return get_attempt(aid)


def activate_attempt(song_id: str, aid: str, audio_path=None, abc_generated=None) -> None:
    """Make a completed attempt the song's current view."""
    fields = dict(active_attempt=aid)
    if audio_path is not None:
        fields["audio_path"] = audio_path
    if abc_generated is not None:
        fields["abc_generated"] = abc_generated
    update_song(song_id, **fields)


def get_attempt(aid: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM generation_attempts WHERE id=?", (aid,)).fetchone()
        return row_to_dict(row) if row else None


def list_attempts(song_id: str) -> list[dict]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM generation_attempts WHERE song_id=?"
                          " ORDER BY attempt_no", (song_id,)).fetchall()
        return [row_to_dict(r) for r in rows]


def update_attempt(aid: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with get_db() as db:
        db.execute(f"UPDATE generation_attempts SET {sets} WHERE id=?", (*fields.values(), aid))


def attempt_output_dir(song_id: str, attempt_id: str, outputs_root: Path) -> Path:
    """v2 layout: outputs/<song-id>/<attempt-id>/ — never shared, never overwritten."""
    return outputs_root / song_id / attempt_id


# ---- transcriptions ----

def create_transcription(source: str, name: str, task: str = "melody_full",
                         melody_only: bool = True) -> dict:
    tid = new_id("trans")
    with get_db() as db:
        db.execute(
            "INSERT INTO transcriptions (id,source,name,status,created_at,task,melody_only)"
            " VALUES (?,?,?,'pending',?,?,?)",
            (tid, source, name, now(), task, int(bool(melody_only))))
    return get_transcription(tid)


def get_transcription(tid: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM transcriptions WHERE id=?", (tid,)).fetchone()
        return row_to_dict(row) if row else None


def list_transcriptions(limit: int = 100) -> list[dict]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM transcriptions ORDER BY created_at DESC LIMIT ?",
                          (limit,)).fetchall()
        return [row_to_dict(r) for r in rows]


def update_transcription(tid: str, **fields) -> None:
    if not fields:
        return
    fields = dict(fields, updated_at=now())
    sets = ", ".join(f"{k}=?" for k in fields)
    with get_db() as db:
        db.execute(f"UPDATE transcriptions SET {sets} WHERE id=?", (*fields.values(), tid))


def delete_transcription(tid: str) -> None:
    job = get_transcription(tid)
    if not job:
        return
    out = job.get("out_dir")
    if out:
        p = Path(out)
        if p.is_dir() and p.parent.name == "runs" and p.name == tid:
            shutil.rmtree(p, ignore_errors=True)
    with get_db() as db:
        db.execute("DELETE FROM transcriptions WHERE id=?", (tid,))
