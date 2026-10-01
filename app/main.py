"""FastAPI app: song generation + transcription UI with SQLite persistence.

v2 semantics (aligned with the YuE2 runtime):
- cot="full": ABC optional (score-conditioned regeneration / reharmonization)
- cot="melody": ABC optional; if present it MUST be chord-free (validated)
- cot="off": no ABC; no score.abc is produced — audio is the completion signal
- Rerun / Variant / Regenerate-from-score are three distinct operations
- Native ABC validation (app.abc_native) is the authority for anything fed
  to YuE2; abcjs is UI preview only
"""
from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import abc_native, db, workers

app = FastAPI(title="YuE Studio")

STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.on_event("startup")
def _startup():
    workers.start_workers()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/editor/{kind}/{sid}")   # legacy shape kept for old links
@app.get("/editor/{sid}")
def editor(sid: str, kind: str = "song"):
    """Serve the score editor for a SONG or a TRANSCRIPTION."""
    return FileResponse(STATIC / "editor.html")


# ---- validation helpers ----

def _validate_request(cot: str, abc: str | None, instrumental: bool):
    """Backend-side YuE2 semantics; never trust the frontend alone."""
    if cot not in {"off", "melody", "full"}:
        raise HTTPException(422, "cot must be off, melody, or full")
    if cot == "off" and (abc or "").strip():
        raise HTTPException(422, "cot=off does not accept an ABC score")
    if abc is not None and not abc.strip():
        abc = None
    if abc is not None:
        inspect = abc_native.quick_inspect(abc)
        if not inspect["valid"]:
            raise HTTPException(422, f"ABC failed native validation: {inspect['error']}")
        if cot == "melody" and inspect["has_chords"]:
            raise HTTPException(422, "This score contains chord symbols; "
                                     "cot=melody requires chord-free ABC. "
                                     "Strip the chords first (POST /api/abc/strip-chords) "
                                     "or use cot=full to keep the harmony.")
    return abc


def _validate_instrumental_abc(abc: str | None):
    if not (abc or "").strip():
        raise HTTPException(422, "Instrumental requires an ABC score")
    inspect = abc_native.quick_inspect(abc)
    if not inspect["valid"]:
        raise HTTPException(422, f"ABC failed native validation: {inspect['error']}")


# ---- generation ----

class SongIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    style: str = Field(min_length=1, max_length=4000)
    lyrics: str = Field(min_length=1, max_length=20000)
    abc: str | None = Field(default=None, max_length=100000)
    cot: str = "full"
    seed: int = Field(default=831001, ge=0, lt=2**63)
    cfg_scale: float | None = Field(default=None, ge=0, le=20)
    instrumental: bool = False
    plan_only: bool = False
    score_source: str | None = None

    model_config = {"str_strip": False}


@app.post("/api/songs")
def create_song(body: SongIn):
    if body.instrumental:
        _validate_instrumental_abc(body.abc)
        # instrumental planning uses YuE2's own cot; chords allowed
        cot = body.cot if body.cot in ("full", "melody") else "full"
    else:
        cot = body.cot
        body.abc = _validate_request(cot, body.abc, body.instrumental)
    song = db.create_song(body.title, body.style, body.lyrics, body.abc, cot,
                          body.seed, cfg_scale=body.cfg_scale,
                          instrumental=body.instrumental,
                          score_source=body.score_source)
    aid = song["active_attempt"]
    if body.plan_only:
        db.update_attempt(aid, kind="plan")
    workers.enqueue_generation(aid)
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


@app.get("/api/songs/{sid}/attempts")
def get_attempts(sid: str):
    if not db.get_song(sid):
        raise HTTPException(404, "song not found")
    return db.list_attempts(sid)


@app.get("/api/songs/{sid}/attempts/{aid}/audio")
def attempt_audio(sid: str, aid: str, download: bool = False):
    """A/B playback of a specific attempt's audio."""
    att = db.get_attempt(aid)
    if not att or att["song_id"] != sid:
        raise HTTPException(404, "attempt not found")
    path = att.get("audio_path")
    if not path or not Path(path).is_file():
        raise HTTPException(404, "attempt audio not ready")
    if download:
        song = db.get_song(sid)
        safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in (song["title"] + f" (attempt {att['attempt_no']})")) or aid
        return FileResponse(path, media_type="audio/flac", filename=f"{safe}.flac",
                            content_disposition_type="attachment")
    return FileResponse(path, media_type="audio/flac")


@app.post("/api/songs/{sid}/attempts/{aid}/activate")
def activate_attempt(sid: str, aid: str):
    """Make an attempt the song's current view (does not delete others)."""
    att = db.get_attempt(aid)
    if not att or att["song_id"] != sid:
        raise HTTPException(404, "attempt not found")
    if att["status"] not in ("done", "done_truncated"):
        raise HTTPException(422, "only completed attempts can be activated")
    db.update_song(sid, active_attempt=aid, audio_path=att["audio_path"],
                   abc_generated=att.get("abc_generated"))
    return {"ok": True}


class RerunIn(BaseModel):
    """Rerun (exact request) / Variant (new seed) / Regenerate (edited ABC)."""
    mode: str = "rerun"           # rerun | variant | regenerate
    abc: str | None = Field(default=None, max_length=100000)
    seed: int | None = Field(default=None, ge=0, lt=2**63)
    cfg_scale: float | None = Field(default=None, ge=0, le=20)
    style: str | None = Field(default=None, max_length=4000)
    lyrics: str | None = Field(default=None, max_length=20000)


@app.post("/api/songs/{sid}/attempts")
def new_attempt(sid: str, body: RerunIn):
    """Create a NEW attempt from the song — never overwrites a baseline.

    - rerun: same request verbatim (same seed).
    - variant: same conditions, fresh random seed.
    - regenerate: from the provided ABC (the edited score), cot per chords.
    """
    song = db.get_song(sid)
    if not song:
        raise HTTPException(404, "song not found")
    base = db.get_attempt(song["active_attempt"]) or db.list_attempts(sid)[0]
    if body.mode not in ("rerun", "variant", "regenerate"):
        raise HTTPException(422, "mode must be rerun, variant, or regenerate")

    style = body.style or base["style"]
    lyrics = body.lyrics or base["lyrics"]
    cfg = body.cfg_scale if body.cfg_scale is not None else base["cfg_scale"]
    seed = base["seed"]

    if body.mode == "variant":
        seed = body.seed if body.seed is not None else random.randrange(2**31)
    elif body.mode == "rerun" and body.seed is not None:
        seed = body.seed

    if body.mode == "regenerate":
        abc = body.abc if (body.abc or "").strip() else (
            song.get("abc_generated") or song.get("abc") or base.get("abc_input"))
        if not (abc or "").strip():
            raise HTTPException(422, "no editable ABC for this song; use rerun or variant")
        abc = _validate_request(base["cot"], abc, False)
        cot = base["cot"]
    else:
        abc = base["abc_input"]
        cot = base["cot"]

    att = db.create_attempt(song_id=sid, kind="generate", title=song["title"],
                            style=style, lyrics=lyrics, cot=cot, seed=seed,
                            cfg_scale=cfg, abc_input=abc,
                            parent_attempt=base["id"],
                            instrumental=bool(base["instrumental"]),
                            score_source=("Edited score" if body.mode == "regenerate"
                                          else base.get("score_source")))
    workers.enqueue_generation(att["id"])
    return att


@app.post("/api/songs/{sid}/regenerate")
def regenerate_song(sid: str):
    """Legacy endpoint — routes to the attempt machinery as Regenerate-from-score."""
    song = db.get_song(sid)
    if not song:
        raise HTTPException(404, "song not found")
    abc = song.get("abc_generated") or song.get("abc")
    if not (abc or "").strip():
        raise HTTPException(422, "song has no ABC to regenerate from; use rerun or variant")
    return new_attempt(sid, RerunIn(mode="regenerate", abc=abc))


class RenderPlanIn(BaseModel):
    """Render audio from a planned attempt, optionally with an edited ABC."""
    abc: str | None = Field(default=None, max_length=100000)


@app.post("/api/songs/{sid}/attempts/{aid}/render")
def render_plan(sid: str, aid: str, body: RenderPlanIn):
    """Plan→Edit→Render: continue a planned attempt into audio.

    - Unedited: continue the saved SymbolicPlan natively (no re-planning).
    - Edited ABC: new attempt kind=generate with the edited ABC as input;
      the original plan directory stays untouched.
    """
    att = db.get_attempt(aid)
    if not att or att["song_id"] != sid:
        raise HTTPException(404, "attempt not found")
    if att["status"] != "planned":
        raise HTTPException(422, f"attempt is not in planned state (status={att['status']})")
    song = db.get_song(sid)
    edited = (body.abc or "").strip()
    if edited and edited != (att.get("abc_generated") or ""):
        edited = _validate_request(att["cot"], edited, False)
        new = db.create_attempt(song_id=sid, kind="generate", title=song["title"],
                                style=att["style"], lyrics=att["lyrics"], cot=att["cot"],
                                seed=att["seed"], cfg_scale=att["cfg_scale"],
                                abc_input=edited, parent_attempt=aid,
                                score_source="Edited score")
        workers.enqueue_generation(new["id"])
        return new
    # unchanged → staged continuation from the immutable plan dir
    db.update_attempt(aid, kind="render", status="pending", error=None)
    workers.enqueue_generation(aid)
    return db.get_attempt(aid)


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
    if db.get_song(sid)["status"] in ("running", "pending"):
        workers.request_cancel(sid)
    db.delete_song(sid, workers.OUTPUTS)
    return {"ok": True}


@app.get("/api/songs/{sid}/audio")
def song_audio(sid: str, download: bool = False):
    """Stream the ACTIVE attempt's FLAC."""
    song = db.get_song(sid)
    if not song or not song.get("audio_path"):
        raise HTTPException(404, "audio not ready")
    if download:
        safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in song["title"]) or sid
        return FileResponse(song["audio_path"], media_type="audio/flac",
                            filename=f"{safe}.flac", content_disposition_type="attachment")
    return FileResponse(song["audio_path"], media_type="audio/flac")


@app.post("/api/songs/{sid}/cancel")
def cancel_song(sid: str):
    """Stop the song. Handles every state:
    - queued: mark its pending attempts cancelled immediately
    - running: cooperative cancel of the live attempt
    - STUCK running (no live attempt — e.g. a crash left the row behind):
      reconcile to the attempt's real state so the UI reacts instead of
      silently doing nothing."""
    song = db.get_song(sid)
    if not song:
        raise HTTPException(404, "song not found")
    attempts = db.list_attempts(sid)
    live = [a for a in attempts if a["status"] in ("pending", "running")]
    if song["status"] in ("running", "pending") and not live:
        # stuck row: reconcile to the active attempt's persisted state
        active = next((a for a in attempts if a["id"] == song["active_attempt"]), None)
        real = active["status"] if active else "failed"
        db.update_song(sid, status=real, error=active.get("error") if active else "reconciled",
                       finished_at=db.now())
        return {"ok": True, "status": f"reconciled → {real}"}
    if not live:
        # already finished — nothing to stop
        return {"ok": True, "status": f"nothing to cancel (status={song['status']})"}
    workers.request_cancel(sid)   # cancels queued rows + targets the running one
    if song["status"] == "pending" or all(a["status"] == "pending" for a in live):
        # queued only: no worker will flip these — set them now
        for a in live:
            db.update_attempt(a["id"], status="cancelled", finished_at=db.now())
        db.update_song(sid, status="cancelled", finished_at=db.now())
        return {"ok": True, "status": "cancelled (was queued)"}
    return {"ok": True, "status": "cancelled"}


# ---- native ABC tools (server-side authority) ----

class AbcText(BaseModel):
    abc: str = Field(min_length=1, max_length=100000)


@app.post("/api/abc/inspect")
def abc_inspect(body: AbcText):
    """Full native inspection: valid?, chords, tempo, meter, voices, duration."""
    try:
        score = abc_native.parse_abc(body.abc)
        rep = abc_native.report(score)
        rep["quick"] = {k: v for k, v in abc_native.quick_inspect(body.abc).items()}
        # strip the heavy note arrays for UI consumption
        rep.pop("voices", None)
        return rep
    except abc_native.AbcError as exc:
        return {"valid": False, "error": str(exc)}


@app.post("/api/abc/strip-chords")
def abc_strip(body: AbcText):
    """Remove ONLY chord annotations; pitches/onsets/durations/meter/tempo verified
    unchanged by native compare. Returns both original and converted ABC."""
    try:
        stripped = abc_native.strip_chords(body.abc)
        before = abc_native.quick_inspect(body.abc)
        after = abc_native.quick_inspect(stripped)
        return {"ok": True, "original_abc": body.abc, "abc": stripped,
                "before": before, "after": after}
    except abc_native.AbcError as exc:
        raise HTTPException(422, f"chord strip failed: {exc}")


@app.post("/api/abc/instrumentalize")
def abc_instrumentalize(body: AbcText):
    """Event-level Vocal→Ins transfer (server-side, not string replacement)."""
    try:
        converted, transfer = abc_native.instrumentalize(body.abc, keep_chords=True)
        return {"ok": True, "abc": converted, "transfer": transfer}
    except abc_native.AbcError as exc:
        raise HTTPException(422, f"instrumentalize failed: {exc}")


# ---- transcription ----

class TransIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    task: str = Field(default="melody_full", pattern="^(melody_full|melody_vocal|chord_full)$")


class FromTrans(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    style: str = Field(min_length=1, max_length=4000)
    lyrics: str = Field(min_length=1, max_length=20000)
    seed: int = Field(default=831001, ge=0, lt=2**63)
    cot: str | None = None          # None → derive from the transcription type


@app.post("/api/transcriptions")
def create_transcription(body: TransIn):
    p = Path(body.path).expanduser()
    if not p.is_file():
        raise HTTPException(422, f"file not found: {p}")
    prompts, melody_only, _ = workers.TRANS_TASKS[body.task]
    job = db.create_transcription(str(p), p.stem, task=body.task, melody_only=melody_only)
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
        workers.request_trans_cancel(tid)
    db.delete_transcription(tid)
    return {"ok": True}


@app.post("/api/songs/from-transcription/{tid}")
def song_from_transcription(tid: str, body: FromTrans):
    """Cover from a transcription. cot follows the transcription type:
    melody transcription → melody (chord-free), full harmony → full."""
    job = db.get_transcription(tid)
    if not job:
        raise HTTPException(404, "transcription not found")
    if job["status"] != "done" or not job.get("abc"):
        raise HTTPException(422, "transcription is not complete")
    task = job.get("task") or "melody_full"
    _, _, default_cot = workers.TRANS_TASKS.get(task, workers.TRANS_TASKS["melody_full"])
    cot = body.cot or default_cot
    abc = _validate_request(cot, job["abc"], False)
    source = ("Transcription full harmony" if cot == "full"
              else "Transcription melody")
    song = db.create_song(body.title, body.style, body.lyrics, abc, cot, body.seed,
                          score_source=source)
    workers.enqueue_generation(song["active_attempt"])
    return song


class AbcUpdate(BaseModel):
    abc: str = Field(min_length=1, max_length=100000)


@app.put("/api/transcriptions/{tid}/abc")
def update_transcription_abc(tid: str, body: AbcUpdate):
    """Persist a hand-edited score on the transcription — the canonical copy."""
    job = db.get_transcription(tid)
    if not job:
        raise HTTPException(404, "transcription not found")
    db.update_transcription(tid, abc=body.abc)
    return {"ok": True}


@app.put("/api/songs/{sid}/abc")
def update_song_abc(sid: str, body: AbcUpdate):
    """Persist a hand-edited score snapshot for an existing song."""
    song = db.get_song(sid)
    if not song:
        raise HTTPException(404, "song not found")
    db.update_song(sid, abc=body.abc)
    return {"ok": True}


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
