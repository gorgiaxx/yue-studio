# YuE Studio

[English](README.md) | [简体中文](README.zh.md)

A local-first web workstation for macOS (Apple Silicon): [YuE2](https://github.com/multimodal-art-projection/YuE) song generation + [SheetSage2](https://huggingface.co/m-a-p/SheetSage2) audio transcription + a visual ABC score editor. Dependencies managed with uv, state persisted in SQLite, everything runs offline.

---

## Architecture

```
yue-studio/
├── app/
│   ├── main.py        FastAPI: full REST API (songs / transcriptions / scores / cancel / rename)
│   ├── workers.py     Background threads: MPS generation (cooperative cancel)
│   │                  + transcription subprocess (kill-able). Interrupted tasks are
│   │                  automatically recovered on restart (running/pending → re-queued).
│   ├── db.py           SQLite persistence (data/studio.db)
│   └── static/         Single-page UI: main app (create / library / queue) + score editor
├── scripts/
│   └── fetch_soundfonts.py   Piano sample fetch script (idempotent, cache-verified — see below)
├── data/               SQLite database (created at runtime, not committed)
├── outputs/            Generated song artifacts (audio.flac / score.abc / latent.npy …)
└── runs/               Transcription outputs (score.abc / *.lab / *.mid)
```

Two model environments must stay separate (conflicting dependency versions): this repo's `.venv` (uv, Python 3.12, torch 2.10 MPS) runs YuE2 generation; `../YuE/.venv-ss2` (Python 3.11, transformers 4.45.2) is invoked as a subprocess by the worker for SheetSage2 transcription. Model weights are shared from `../hf-cache` (YuE2-3B, YuE2-Vae, MERT2).

## Quick Start

```bash
# 1. First run: fetch the piano samples (~2.1 MB; source & license below)
python3 scripts/fetch_soundfonts.py

# 2. Install dependencies (uv)
uv sync

# 3. Start (also requires ../YuE/.venv-ss2 for transcription — see the YuE repo README)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8770
# open http://127.0.0.1:8770
```

## Features

**Create** — title / style prompt / lyrics (`[Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Outro]` section tags; `[la]` for humming) / seed. Three modes: *Smart Arrangement* (AI plans melody + harmony), *Melody Guided* (re-arrange an existing ABC score — pick one from the built-in library dropdown), *Direct* (skip score planning).

**Score workflow** — scores belong to transcriptions; songs are derived from scores:
```
audio file ─transcribe→ 🎼 score (transcription asset) ─spawn→ 🎵 song₁ 🎵 song₂ …
                        ↑ refine in the editor (save writes back to the canonical copy)
```
- One transcription score can spawn any number of covers (picker / editor "new song" dialog / main-page button)
- Each song retains its own score snapshot, individually editable

**Score editor** (`/editor/trans/{id}` or `/editor/{sid}`) —
- Left: ABC source. Right: live staff rendering. Bidirectional cursor sync across click / seek / playback
- Playback preview: MIDI piano synthesis + draggable seek bar + purple note highlight following the playhead + code-pane focus sync + play-from-caret
- Pro shortcuts: `←→` note navigation, `↑↓` semitone (Shift = octave), `1–8` duration, `R` repeat, `Backspace` to rest, `Space` play, `⌘Z / ⌘⇧Z` undo/redo, `?` help panel
- Interactions: drag a note to change pitch, right-click menu (pitch / octave / duration / rest / delete), double-click to audition

**Task management** — running/pending tasks can be stopped (generation uses cooperative cancellation, transcription kills the subprocess); cancelled tasks can be re-queued; interrupted tasks are automatically recovered when the server restarts.

**Persistence** — everything lives in SQLite + on-disk artifacts and survives restarts. Cards show created/modified timestamps; titles rename by double-click.

## Performance

Measured on M4 Pro (48 GB):

- Transcribe a ~97 s audio file: ≈ 1.5 min
- Generate (melody mode, 96 s output): ≈ 5.5 min; direct mode (213 s output): ≈ 22 min
- The queue is serial (one task at a time)

## API

```
POST   /api/songs                          create generation job {title, style, lyrics, cot, abc?, seed}
GET    /api/songs, /api/songs/{id}         list / detail
PATCH  /api/songs/{id}                     rename {title}
DELETE /api/songs/{id}                     delete (incl. artifacts)
POST   /api/songs/{id}/cancel              stop / dequeue
POST   /api/songs/{id}/regenerate          re-generate from the current score
PUT    /api/songs/{id}/abc                 persist score snapshot
GET    /api/songs/{id}/audio               audio (FLAC)
POST   /api/transcriptions                 submit transcription {path}
GET    /api/transcriptions, /{id}          list / detail
PATCH  /api/transcriptions/{id}            rename
PUT    /api/transcriptions/{id}/abc        persist canonical score
POST   /api/transcriptions/{id}/cancel     stop
POST   /api/songs/from-transcription/{id}  spawn a song from a score {title, style, lyrics, seed}
GET    /editor/trans/{tid} | /editor/{sid} score editor pages
```

## Piano Samples

The editor's MIDI preview needs 89 piano note samples (FluidR3_GM acoustic grand piano, ≈ 2.1 MB total). **The samples are NOT distributed with this repository** — a script downloads them on demand into `app/static/soundfonts/` (gitignored):

```bash
python3 scripts/fetch_soundfonts.py          # download / top up the cache (idempotent)
python3 scripts/fetch_soundfonts.py --check  # verify only; exit code usable in CI
```

- The editor probes the cache before playback and shows an actionable hint if it is missing (no silent failure)
- The script fetches exactly the notes abcjs requests (black keys use flat names: Bb/Db/Eb/Gb/Ab)

If the script cannot run in a restricted network, run it on any connected machine and copy the whole `app/static/soundfonts/` directory over.

### Sample source & license

| Asset | Source | License |
|---|---|---|
| FluidR3_GM acoustic_grand_piano-mp3 | [midi-js-soundfonts](https://github.com/paulrosen/midi-js-soundfonts) (pre-converted mirror) | **MIT**; derived from the FluidR3_GM SoundFont (FluidSynth community) |
| abcjs (score rendering/synthesis JS lib, vendored in `app/static/vendor/`) | [abcjs](https://github.com/paulrosen/abcjs) | **MIT** |
| YuE2 inference runtime | [multimodal-art-projection/YuE](https://github.com/multimodal-art-projection/YuE) | Apache-2.0 (weights: see its MODEL_LICENSE) |
| YuE2-3B / YuE2-Vae / SheetSage2 / MERT2 weights | [m-a-p on Hugging Face](https://huggingface.co/m-a-p) | per model card |

## Open Source Statement

This repository's own code is released under the **Apache License 2.0** (see [LICENSE](LICENSE)). Third-party components and their licenses:

- **YuE** (Apache-2.0) — lyrics-to-song generation pipeline and symbolic planning; model weights follow their MODEL_LICENSE, local inference only
- **SheetSage2 / MERT2** (per model card) — audio-to-melody/chords/meter transcription
- **abcjs** (MIT) — staff rendering, interaction, MIDI synthesis
- **midi-js-soundfonts / FluidR3_GM** (MIT) — audition samples
- **FastAPI / uvicorn / Pydantic** (MIT/BSD) — web service
- **PyTorch** (BSD-style) — MPS inference

Rights to generated music are governed by the underlying model licenses — read each model card before use. This repository claims no rights over generated output.

## Data & Privacy

All processing happens on your machine: SQLite, audio artifacts and model caches stay on local disk. No telemetry, no external reporting. The only outbound network access is the one-time sample download from GitHub Pages.
