"""Background workers: YuE2 generation and SheetSage2 transcription.

Design (v2):
- Every generation is an immutable ATTEMPT (outputs/<song>/<attempt>/).
- cot semantics per YuE2: full/melody may take optional ABC; off must not.
  cot="off" produces no score.abc — audio.flac is the completion condition.
- Instrumental is a workflow, not a cot: plan (or supplied ABC) → Vocal→Ins
  transfer (abc_native.instrumentalize) → regenerate with converted ABC.
- Plan-first: an attempt may stop after pipe.plan(...) as kind="plan",
  then be rendered later from the saved plan directory.
- save_artifacts' receipt (result.json) is parsed: truncation flags surface
  as status="done_truncated" (Needs Review), never silently "done".
- cfg_scale: pass through only when the user set it; None → runtime default.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from pathlib import Path

from . import abc_native, db

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
HF_HOME = "/Volumes/intel760p/music_projects/hf-cache"
SHEETSAGE2_DIR = "/Volumes/intel760p/music_projects/YuE/models/SheetSage2"

os.environ.setdefault("HF_HOME", HF_HOME)

_gen_queue: queue.Queue = queue.Queue()
_trans_queue: queue.Queue = queue.Queue()

# Loaded lazily, one at a time; never both resident (unified memory).
_pipe_lock = threading.Lock()
_pipe = None  # YuE2Pipeline


def _get_pipe():
    global _pipe
    if _pipe is None:
        from yue2 import YuE2Pipeline
        hub = Path(HF_HOME) / "hub"
        model_dir = next((hub / "models--m-a-p--YuE2-3B" / "snapshots").glob("*"))
        vae_dir = next((hub / "models--m-a-p--YuE2-Vae" / "snapshots").glob("*"))
        _pipe = YuE2Pipeline.from_pretrained(model_dir, vae=vae_dir, device="mps",
                                             local_files_only=True)
    return _pipe


def _release_pipe():
    global _pipe
    with _pipe_lock:
        _pipe = None

_CANCEL = threading.Event()   # set → the currently running generation aborts


def enqueue_generation(aid: str) -> None:
    _gen_queue.put(aid)


def enqueue_transcription(tid: str) -> None:
    _trans_queue.put(tid)


def request_cancel(sid: str) -> bool:
    """Mark a running song as cancelled. Returns True if the song is cancellable."""
    _CANCEL.set()
    return True


def request_trans_cancel(tid: str) -> bool:
    """Kill the SheetSage2 subprocess for a running transcription."""
    proc = _trans_proc.get("proc")
    if proc is not None and proc.poll() is None:
        proc.kill()
    return True


_trans_proc: dict = {}


# ---- attempt execution ----

def _runtime_version() -> str:
    try:
        import importlib.metadata as md
        return md.version("yue2-infer")
    except Exception:  # noqa: BLE001
        return "unknown"


def _finish_attempt(aid, status, error=None, extra=None):
    fields = dict(status=status, finished_at=db.now())
    if error is not None:
        fields["error"] = f"{error}"
    if extra:
        fields.update(extra)
    db.update_attempt(aid, **fields)
    latest = db.get_attempt(aid)
    song = db.get_song(latest["song_id"])
    if not song:
        return
    terminal = status in ("done", "done_truncated", "failed", "cancelled", "planned")
    # The song's status tracks its ACTIVE attempt; other queued attempts show
    # through the queue strip. A newly completed non-active attempt becomes
    # active automatically (it superseded the previous view).
    if song["active_attempt"] == aid:
        db.update_song(song["id"], status=status, error=error)
    elif terminal and status in ("done", "done_truncated", "planned"):
        # a later attempt finished while an older one is still viewed:
        # surface the finished one (queue order guarantees recency)
        db.activate_attempt(song["id"], aid,
                            audio_path=latest.get("audio_path"),
                            abc_generated=latest.get("abc_generated"))
        db.update_song(song["id"], status=status, error=error)
    elif song["status"] in ("pending", "running") and status in ("failed", "cancelled"):
        db.update_song(song["id"], status=status, error=error)


def _attempt(aid):
    return db.get_attempt(aid)


def _apply_receipt(aid: str, out: Path, receipt: dict, cfg_used) -> dict:
    """Extract durable facts from save_artifacts' result.json into the attempt row."""
    truncated = receipt.get("truncated") or {}
    weights = receipt.get("weights") or {}
    mot = weights.get("mot", {})
    vae = weights.get("vae", {})
    config = {}
    cfg_file = out / "config.json"
    if cfg_file.is_file():
        try:
            config = json.loads(cfg_file.read_text())
        except (OSError, json.JSONDecodeError):
            config = {}
    extra = dict(
        audio_seconds=receipt.get("audio_seconds"),
        truncated_abc=int(bool(truncated.get("abc"))),
        truncated_semantic=int(bool(truncated.get("semantic"))),
        model="m-a-p/YuE2-3B",
        model_revision=mot.get("revision"),
        vae="m-a-p/YuE2-Vae",
        vae_revision=vae.get("revision"),
        runtime_version=_runtime_version(),
        cfg_scale=cfg_used,
        config_meta=json.dumps({
            "cfg_scale": config.get("cfg_scale"),
            "ode_steps": (config.get("generation") or {}).get("ode_steps"),
            "context": (config.get("generation") or {}).get("context"),
            "model_dtype": config.get("model_dtype"),
            "vae_dtype": config.get("vae_dtype"),
            "device": config.get("device"),
            "runtime_sha256": config.get("runtime_sha256"),
        }),
    )
    return extra


def _run_generation(aid: str) -> None:
    att = _attempt(aid)
    if not att:
        return
    if att["status"] == "cancelled":
        return                      # cancelled while queued
    _CANCEL.clear()
    song_id = att["song_id"]
    out = db.attempt_output_dir(song_id, aid, OUTPUTS)
    try:
        db.update_attempt(aid, status="running")
        db.update_song(song_id, status="running", error=None)
        cot, instrumental = att["cot"], bool(att["instrumental"])
        abc_input = att["abc_input"]
        style, lyrics, seed = att["style"], att["lyrics"], att["seed"]

        # ---- Instrumental workflow: convert the score BEFORE generation ----
        plan_dir = att["plan_dir"]
        if instrumental:
            if abc_input is None:
                raise ValueError("Instrumental requires an ABC score (planned or supplied)")
            conv, transfer = abc_native.instrumentalize(abc_input, keep_chords=True)
            (out / "conversion").mkdir(parents=True, exist_ok=True)
            (out / "conversion" / "original.abc").write_text(abc_input, encoding="utf-8")
            (out / "conversion" / "instrumental.abc").write_text(conv, encoding="utf-8")
            (out / "conversion" / "transfer.json").write_text(
                json.dumps(transfer, ensure_ascii=False, indent=2), encoding="utf-8")
            style = abc_native.instrumental_style(style)
            lyrics = abc_native.instrumental_lyrics(conv) or lyrics
            # Post-transfer: Vocal has no sounding notes; cot follows chord presence.
            score = abc_native.parse_abc(conv)
            abc_input = conv
            cot = "full" if score.voices["Vocal"].chords else "melody"
            db.update_attempt(aid, cot=cot, style=style, lyrics=lyrics)

        from yue2.protocol import SongRequest
        kwargs = dict(id=aid.replace("song-", "s", 1), style=style, lyrics=lyrics,
                      cot=cot, seed=seed)
        if abc_input is not None:
            kwargs["abc"] = abc_input
        if att["cfg_scale"] is not None:
            kwargs["cfg_scale"] = float(att["cfg_scale"])
        request = SongRequest(**kwargs)

        with _pipe_lock:
            pipe = _get_pipe()

            # ---- Plan-first: render from a saved plan when continuing ----
            if plan_dir and Path(plan_dir).is_dir() and att["kind"] == "render":
                from yue2 import SymbolicPlan
                plan = SymbolicPlan.load(plan_dir)
                semantic = pipe.generate_semantic(plan, cancelled=_CANCEL.is_set)
                latents = pipe.synthesize(semantic, cancelled=_CANCEL.is_set)
                if _CANCEL.is_set():
                    raise InterruptedError
                audio = pipe.decode(latents)
                from yue2.pipeline import SongResult
                config = pipe.effective_config(plan.request)
                result = SongResult(audio, 48000, semantic, latents, config,
                                    pipe.weights, {}, "resumed")
                receipt = result.save_artifacts(out)
            else:
                result = pipe(request=request, cancelled=_CANCEL.is_set)
                # save the untouched plan for plan-first workflows
                result.semantic.plan.save(out)
                receipt = result.save_artifacts(out)

        if _CANCEL.is_set():
            _finish_attempt(aid, "cancelled")
            print(f"[gen] {aid} cancelled")
            return

        # score.abc exists only for full/melody — never assume it for off.
        abc_generated = None
        score_file = out / "score.abc"
        if score_file.is_file():
            abc_generated = score_file.read_text(encoding="utf-8")

        cfg_used = json.loads(json.dumps(
            (json.loads((out / "config.json").read_text()) if (out / "config.json").is_file()
             else {}) or "{}")).get("cfg_scale")
        extra = _apply_receipt(aid, out, receipt, cfg_used)
        truncated = bool(extra["truncated_abc"]) or bool(extra["truncated_semantic"])
        status = "done_truncated" if truncated else "done"
        audio_path = str(out / "audio.flac")
        _finish_attempt(aid, status, extra=dict(
            extra, abc_generated=abc_generated, audio_path=audio_path,
            output_dir=str(out)))
        if truncated:
            print(f"[gen] {aid} completed but TRUNCATED (needs review)")
    except InterruptedError:
        _finish_attempt(aid, "cancelled")
        print(f"[gen] {aid} cancelled")
    except Exception as exc:  # noqa: BLE001 - worker boundary
        if _CANCEL.is_set():
            _finish_attempt(aid, "cancelled")
        else:
            _finish_attempt(aid, "failed", error=exc)
            traceback.print_exc()
    finally:
        _release_pipe()
        _CANCEL.clear()


def _run_plan_only(aid: str) -> None:
    """Plan-first: run pipe.plan() and stop. The saved plan dir is immutable."""
    att = _attempt(aid)
    if not att or att["status"] == "cancelled":
        return
    _CANCEL.clear()
    song_id = att["song_id"]
    out = db.attempt_output_dir(song_id, aid, OUTPUTS)
    try:
        db.update_attempt(aid, status="running")
        db.update_song(song_id, status="running", error=None)
        from yue2.protocol import SongRequest
        kwargs = dict(id=aid.replace("song-", "s", 1), style=att["style"],
                      lyrics=att["lyrics"], cot=att["cot"], seed=att["seed"])
        if att["abc_input"] is not None:
            kwargs["abc"] = att["abc_input"]
        if att["cfg_scale"] is not None:
            kwargs["cfg_scale"] = float(att["cfg_scale"])
        request = SongRequest(**kwargs)
        with _pipe_lock:
            pipe = _get_pipe()
            plan = pipe.plan(request=request, cancelled=_CANCEL.is_set)
            plan.save(out)
            provenance = {"composer": "YuE2", "weights": pipe.weights,
                          "config": pipe.effective_config(request),
                          "runtime_version": _runtime_version()}
        (out / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        if _CANCEL.is_set():
            _finish_attempt(aid, "cancelled")
            return
        abc_generated = None
        if plan.abc:
            abc_generated = plan.abc
        _finish_attempt(aid, "planned", extra=dict(
            abc_generated=abc_generated, output_dir=str(out), plan_dir=str(out),
            runtime_version=_runtime_version(),
            truncated_abc=int(bool(plan.truncated))))
        print(f"[plan] {aid} planned → {out}")
    except InterruptedError:
        _finish_attempt(aid, "cancelled")
    except Exception as exc:  # noqa: BLE001
        if _CANCEL.is_set():
            _finish_attempt(aid, "cancelled")
        else:
            _finish_attempt(aid, "failed", error=exc)
            traceback.print_exc()
    finally:
        _release_pipe()
        _CANCEL.clear()


# ---- SheetSage2 transcription ----

TRANS_TASKS = {
    # label → (SheetSage2 prompts tuple, melody_only, YuE2 cover cot)
    "melody_full": (("timestamp", "downbeat_meter", "structure", "key", "melody_full"), True, "melody"),
    "melody_vocal": (("timestamp", "downbeat_meter", "structure", "key", "melody_vocal"), True, "melody"),
    "chord_full": (("timestamp", "downbeat_meter", "structure", "key", "chord_full", "melody_full"), False, "full"),
}


def _run_transcription(tid: str) -> None:
    job = db.get_transcription(tid)
    if not job:
        return
    try:
        db.update_transcription(tid, status="running")
        # SheetSage2 pins transformers/numpy versions incompatible with the
        # yue2 venv, so it runs in its own interpreter.
        import subprocess
        task = job["task"] or "melody_full"
        prompts, melody_only, _cot = TRANS_TASKS.get(task, TRANS_TASKS["melody_full"])
        out_dir = ROOT / "runs" / job["id"]
        script = (
            "import sys, json\n"
            f"model_dir = {SHEETSAGE2_DIR!r}\n"
            "from transformers import AutoModel\n"
            "model = AutoModel.from_pretrained(model_dir, trust_remote_code=True).eval().to('mps')\n"
            f"result = model.transcribe({job['source']!r}, output_dir={str(out_dir)!r}, dtype='fp32',"
            f" prompts={prompts!r}, melody_only={melody_only!r})\n"
            "print('::RESULT::' + json.dumps({'abc': result.get('abc'),"
            " 'error': result.get('abc_error'), 'warnings': result.get('warnings'),"
            " 'key': result.get('labs', {}).get('key'), 'duration': result.get('duration_seconds')}))\n"
        )
        venv_py = "/Volumes/intel760p/music_projects/YuE/.venv-ss2/bin/python"
        _trans_proc["proc"] = subprocess.Popen([venv_py, "-c", script], stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, text=True,
                                               env={**os.environ, "HF_HOME": HF_HOME})
        stdout, stderr = _trans_proc["proc"].communicate()
        proc = _trans_proc["proc"]
        if proc.returncode != 0 and db.get_transcription(tid)["status"] == "cancelled":
            print(f"[trans] {tid} cancelled")
            return
        payload = None
        for line in stdout.splitlines():
            if line.startswith("::RESULT::"):
                payload = json.loads(line[len("::RESULT::"):])
        if payload is None:
            raise RuntimeError(f"SheetSage2 failed: {stderr.strip()[-2000:]}")
        if payload.get("error") or not payload.get("abc"):
            raise RuntimeError(f"No usable ABC: {payload.get('error')}")
        key = payload.get("key") or ""
        duration = payload.get("duration") or 0.0
        warnings = payload.get("warnings") or []
        # result.json schema uses integer event stats; keep the full result too
        meta = {}
        result_file = out_dir / "result.json"
        if result_file.is_file():
            try:
                meta = json.loads(result_file.read_text())
            except (OSError, json.JSONDecodeError):
                meta = {}
        db.update_transcription(tid, status="done", abc=payload["abc"], key=key,
                                duration=duration, out_dir=str(out_dir),
                                warnings=json.dumps(warnings, ensure_ascii=False),
                                meta=json.dumps({
                                    "task": task, "melody_only": melody_only,
                                    "events": meta.get("events"),
                                    "model": "m-a-p/SheetSage2",
                                    "dtype": "fp32",
                                    "peak_gpu_mib": meta.get("peak_gpu_mib"),
                                    "windows": meta.get("windows"),
                                }, ensure_ascii=False),
                                finished_at=db.now())
        print(f"[trans] {tid} done ({len(warnings)} warning(s))")
    except Exception as exc:  # noqa: BLE001 - worker boundary
        db.update_transcription(tid, status="failed", error=f"{exc}", finished_at=db.now())
        traceback.print_exc()


def _worker(q, fn, name):
    while True:
        item = q.get()
        try:
            fn(item)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            q.task_done()


def recover_interrupted_tasks() -> None:
    """Re-queue attempts/transcriptions that a restart left mid-flight.

    Attempt-level recovery: running/pending attempts are reset to pending and
    re-queued (kind routes them: plan attempts → plan worker path, others →
    full generation). Everything needed to rerun is persisted in SQLite.
    """
    for s in db.list_songs(limit=1000):
        for a in db.list_attempts(s["id"]):
            if a["status"] in ("running", "pending"):
                db.update_attempt(a["id"], status="pending", error=None)
                db.update_song(s["id"], status="pending", error=None)
                _gen_queue.put(a["id"])
                print(f"[recover] re-queued attempt {a['id']} (kind={a['kind']})")


def start_workers() -> None:
    OUTPUTS.mkdir(exist_ok=True)
    (ROOT / "runs").mkdir(exist_ok=True)
    recover_interrupted_tasks()
    threading.Thread(target=_worker,
                     args=(_gen_queue, _gen_dispatch, "gen"),
                     daemon=True, name="yue-gen").start()
    threading.Thread(target=_worker,
                     args=(_trans_queue, _run_transcription, "trans"),
                     daemon=True, name="yue-trans").start()


def _gen_dispatch(aid: str) -> None:
    att = db.get_attempt(aid)
    if att and att["kind"] in ("plan", "render_plan"):
        _run_plan_only(aid)
    else:
        _run_generation(aid)
