"""Background workers: YuE2 generation and SheetSage2 transcription.

Two independent single-slot queues, each a daemon thread. Models load lazily
inside the worker so the web UI starts instantly.
"""
from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
HF_HOME = "/Volumes/intel760p/music_projects/hf-cache"
SHEETSAGE2_DIR = "/Volumes/intel760p/music_projects/YuE/models/SheetSage2"

import os
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
        _pipe = YuE2Pipeline.from_pretrained("m-a-p/YuE2-3B", device="mps",
                                             memory_budget_gib=40, progress=False)
    return _pipe


def _release_pipe():
    global _pipe
    if _pipe is not None:
        _pipe.close()
        _pipe = None


def enqueue_generation(sid: str) -> None:
    _gen_queue.put(sid)


def enqueue_transcription(tid: str) -> None:
    _trans_queue.put(tid)


def _run_generation(sid: str) -> None:
    song = db.get_song(sid)
    if not song:
        return
    try:
        db.update_song(sid, status="running")
        with _pipe_lock:
            pipe = _get_pipe()
            result = pipe(
                style=song["style"],
                lyrics=song["lyrics"],
                abc=song["abc"] or None,
                cot=song["cot"],
                seed=song["seed"],
            )
            out = OUTPUTS / f"song-{sid}"
            result.save_artifacts(out)
        db.update_song(sid, status="done",
                       audio_path=str(out / "audio.flac"),
                       abc_generated=out.joinpath("score.abc").read_text(encoding="utf-8"),
                       finished_at=db.now())
    except Exception as exc:  # noqa: BLE001 - worker boundary
        db.update_song(sid, status="failed", error=f"{exc}", finished_at=db.now())
        traceback.print_exc()
    finally:
        _release_pipe()


def _run_transcription(tid: str) -> None:
    job = db.get_transcription(tid)
    if not job:
        return
    try:
        db.update_transcription(tid, status="running")
        # SheetSage2 pins transformers/numpy versions incompatible with the
        # yue2 venv, so it runs in its own interpreter.
        import subprocess, sys
        out_dir = ROOT / "runs" / job["id"]
        script = (
            "import sys, json\n"
            f"model_dir = {SHEETSAGE2_DIR!r}\n"
            "from transformers import AutoModel\n"
            "model = AutoModel.from_pretrained(model_dir, trust_remote_code=True).eval().to('mps')\n"
            f"result = model.transcribe({job['source']!r}, output_dir={str(out_dir)!r}, dtype='fp32', melody_only=True)\n"
            "print('::RESULT::' + json.dumps({'abc': result.get('abc'), 'error': result.get('abc_error')}))\n"
        )
        venv_py = "/Volumes/intel760p/music_projects/YuE/.venv-ss2/bin/python"
        proc = subprocess.run([venv_py, "-c", script], capture_output=True, text=True,
                              env={**os.environ, "HF_HOME": HF_HOME})
        payload = None
        for line in proc.stdout.splitlines():
            if line.startswith("::RESULT::"):
                import json as _json
                payload = _json.loads(line[len("::RESULT::"):])
        if payload is None:
            raise RuntimeError(f"SheetSage2 failed: {proc.stderr.strip()[-2000:]}")
        if payload.get("error") or not payload.get("abc"):
            raise RuntimeError(f"No usable ABC: {payload.get('error')}")
        key = ""
        key_file = out_dir / "key.lab"
        if key_file.is_file():
            key = key_file.read_text(encoding="utf-8").split("\t")[-1].strip()
        duration = 0.0
        result_file = out_dir / "result.json"
        if result_file.is_file():
            import json as _json
            duration = _json.loads(result_file.read_text()).get("duration_seconds", 0.0)
        db.update_transcription(tid, status="done", abc=payload["abc"], key=key,
                                duration=duration, out_dir=str(out_dir),
                                finished_at=db.now())
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


def start_workers() -> None:
    OUTPUTS.mkdir(exist_ok=True)
    (ROOT / "runs").mkdir(exist_ok=True)
    threading.Thread(target=_worker, args=(_gen_queue, _run_generation, "gen"),
                     daemon=True, name="yue-gen").start()
    threading.Thread(target=_worker, args=(_trans_queue, _run_transcription, "trans"),
                     daemon=True, name="yue-trans").start()
