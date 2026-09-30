"""SQLite persistence for songs, transcriptions, and generation settings."""
import json
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
    finished_at REAL
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
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_songs_created ON songs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trans_created ON transcriptions(created_at DESC);
"""


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ---- songs ----

def create_song(title, style, lyrics, abc, cot, seed) -> dict:
    sid = new_id("song")
    t = now()
    with get_db() as db:
        db.execute(
            "INSERT INTO songs (id,title,style,lyrics,abc,cot,seed,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,'pending',?,?)",
            (sid, title, style, lyrics, abc, cot, seed, t, t))
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
    if song.get("audio_path"):
        p = Path(song["audio_path"])
        if p.is_file():
            p.unlink()
        out = p.parent if p.suffix == ".flac" else None
        if out and out.name.startswith("song-") and out.is_dir():
            import shutil
            shutil.rmtree(out, ignore_errors=True)
    with get_db() as db:
        db.execute("DELETE FROM songs WHERE id=?", (sid,))


# ---- transcriptions ----

def create_transcription(source: str, name: str) -> dict:
    tid = new_id("trans")
    with get_db() as db:
        db.execute(
            "INSERT INTO transcriptions (id,source,name,status,created_at)"
            " VALUES (?,?,?,'pending',?)",
            (tid, source, name, now()))
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
            import shutil
            shutil.rmtree(p, ignore_errors=True)
    with get_db() as db:
        db.execute("DELETE FROM transcriptions WHERE id=?", (tid,))
