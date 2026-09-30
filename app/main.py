"""FastAPI app: song generation + transcription UI with SQLite persistence."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, workers

app = FastAPI(title="YuE Studio")

STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.on_event("startup")
def _startup():
    workers.start_workers()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ---- generation ----


@app.get("/editor/{kind}/{sid}")   # legacy shape kept for old links
@app.get("/editor/{sid}")
def editor(sid: str, kind: str = "song"):
    """Serve the score editor for a SONG or a TRANSCRIPTION.

    Scores are primarily owned by transcriptions (one score → many songs);
    the song route remains for re-editing a song's own generated score."""
    if kind not in ("song", "trans"):
        raise HTTPException(404, "unknown editor target")
    exists = db.get_song(sid) if kind == "song" else db.get_transcription(sid)
    if not exists:
        raise HTTPException(404, f"{kind} not found")
    return FileResponse(STATIC / "editor.html")


class SongIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    style: str = Field(min_length=1, max_length=2000)
    lyrics: str = Field(min_length=1, max_length=20000)
    abc: str | None = Field(default=None, max_length=100000)
    cot: str = "full"
    seed: int = Field(default=831001, ge=0, lt=2**63)

    model_config = {"str_strip": False}


@app.post("/api/songs")
def create_song(body: SongIn):
    if body.cot not in {"off", "melody", "full"}:
        raise HTTPException(422, "cot must be off, melody, or full")
    if body.abc is not None and (body.cot == "off" or not body.abc.strip()):
        raise HTTPException(422, "abc requires cot=melody/full and nonempty text")
    song = db.create_song(body.title, body.style, body.lyrics, body.abc, body.cot, body.seed)
    workers.enqueue_generation(song["id"])
    return song


@app.get("/api/songs")
def list_songs():
    return db.list_songs()


@app.get("/api/songs/{sid}")
def get_song(sid: str):
    song = db.get_song(sid)
    if not song:
        raise HTTPException(404, "song not found")
    return song



class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@app.patch("/api/songs/{sid}")
def rename_song(sid: str, body: RenameIn):
    if not db.get_song(sid):
        raise HTTPException(404, "song not found")
    db.update_song(sid, title=body.title)
    return {"ok": True}


@app.delete("/api/songs/{sid}")
def delete_song(sid: str):
    if not db.get_song(sid):
        raise HTTPException(404, "song not found")
    db.delete_song(sid, workers.OUTPUTS)
    return {"ok": True}


@app.get("/api/songs/{sid}/audio")
def song_audio(sid: str, download: bool = False):
    """Stream the FLAC. With ?download=1 respond as an attachment named after the title
    (sanitized); otherwise serve inline so the <audio> element and the <a download>
    attribute control naming and playback respectively."""
    song = db.get_song(sid)
    if not song or not song.get("audio_path"):
        raise HTTPException(404, "audio not ready")
    if download:
        safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in song["title"]) or sid
        return FileResponse(song["audio_path"], media_type="audio/flac",
                            filename=f"{safe}.flac", content_disposition_type="attachment")
    return FileResponse(song["audio_path"], media_type="audio/flac")


# ---- transcription ----

class TransIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class FromTrans(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    style: str = Field(min_length=1, max_length=2000)
    lyrics: str = Field(min_length=1, max_length=20000)
    seed: int = Field(default=831001, ge=0, lt=2**63)


@app.post("/api/transcriptions")
def create_transcription(body: TransIn):
    p = Path(body.path).expanduser()
    if not p.is_file():
        raise HTTPException(422, f"file not found: {p}")
    job = db.create_transcription(str(p.resolve()), p.stem)
    workers.enqueue_transcription(job["id"])
    return job


@app.get("/api/transcriptions")
def list_transcriptions():
    return db.list_transcriptions()


@app.get("/api/transcriptions/{tid}")
def get_transcription(tid: str):
    job = db.get_transcription(tid)
    if not job:
        raise HTTPException(404, "transcription not found")
    return job


@app.patch("/api/transcriptions/{tid}")
def rename_transcription(tid: str, body: RenameIn):
    if not db.get_transcription(tid):
        raise HTTPException(404, "transcription not found")
    db.update_transcription(tid, name=body.title)
    return {"ok": True}


@app.delete("/api/transcriptions/{tid}")
def delete_transcription(tid: str):
    job = db.get_transcription(tid)
    if not job:
        raise HTTPException(404, "transcription not found")
    if job["status"] in ("running", "pending"):
        raise HTTPException(409, "stop the transcription before deleting it")
    db.delete_transcription(tid)
    return {"ok": True}


@app.post("/api/songs/from-transcription/{tid}")
def song_from_transcription(tid: str, body: FromTrans):
    job = db.get_transcription(tid)
    if not job:
        raise HTTPException(404, "transcription not found")
    if job["status"] != "done" or not job.get("abc"):
        raise HTTPException(409, "transcription not finished")
    song = db.create_song(body.title, body.style, body.lyrics, job["abc"],
                          "melody", body.seed)
    workers.enqueue_generation(song["id"])
    return song


class AbcUpdate(BaseModel):
    abc: str = Field(min_length=1, max_length=100000)


@app.put("/api/transcriptions/{tid}/abc")
def update_transcription_abc(tid: str, body: AbcUpdate):
    """Persist a hand-edited score on the transcription — the canonical copy."""
    if not db.get_transcription(tid):
        raise HTTPException(404, "transcription not found")
    db.update_transcription(tid, abc=body.abc)
    return {"ok": True}




@app.put("/api/songs/{sid}/abc")
def update_song_abc(sid: str, body: AbcUpdate):
    """Persist a hand-edited score for an existing song."""
    if not db.get_song(sid):
        raise HTTPException(404, "song not found")
    db.update_song(sid, abc=body.abc)
    return {"ok": True}


@app.post("/api/songs/{sid}/regenerate")
def regenerate_song(sid: str):
    """Re-queue generation with the song's current (possibly edited) ABC."""
    song = db.get_song(sid)
    if not song:
        raise HTTPException(404, "song not found")
    if not (song["abc"] or "").strip():
        raise HTTPException(422, "song has no ABC to regenerate from; use melody/full mode")
    db.update_song(sid, status="pending", error=None, audio_path=None, abc_generated=None)
    workers.enqueue_generation(sid)
    return {"ok": True}


@app.post("/api/songs/{sid}/cancel")
def cancel_song(sid: str):
    """Stop a running (or dequeue a pending) song generation."""
    song = db.get_song(sid)
    if not song:
        raise HTTPException(404, "song not found")
    if song["status"] == "pending":
        db.update_song(sid, status="cancelled", finished_at=db.now())
        return {"ok": True, "status": "cancelled (was queued)"}
    if not workers.request_cancel(sid):
        raise HTTPException(409, f"song is not running (status={song['status']})")
    return {"ok": True, "status": "cancelled"}


@app.post("/api/transcriptions/{tid}/cancel")
def cancel_transcription(tid: str):
    """Stop a running (or dequeue a pending) transcription."""
    job = db.get_transcription(tid)
    if not job:
        raise HTTPException(404, "transcription not found")
    if job["status"] == "pending":
        db.update_transcription(tid, status="cancelled", finished_at=db.now())
        return {"ok": True, "status": "cancelled (was queued)"}
    if not workers.request_trans_cancel(tid):
        raise HTTPException(409, f"transcription is not running (status={job['status']})")
    return {"ok": True, "status": "cancelled"}
