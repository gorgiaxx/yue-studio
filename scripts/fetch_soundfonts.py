#!/usr/bin/env python3
"""Download the FluidR3_GM piano soundfont used by the score editor's MIDI preview.

abcjs (MIT) fetches per-note MP3 samples at runtime from a configurable
soundFontUrl. This script mirrors the FluidR3_GM acoustic grand piano set
(89 notes, ~2.1 MB) into the app's cache directory so the editor works offline.

Samples originate from the midi-js-soundfonts project:
  https://github.com/paulrosen/midi-js-soundfonts
  FluidR3_GM, acoustic_grand_piano-mp3, converted from the FluidR3_GM
  SoundFont (FluidSynth project, MIT license; see their repository for
  attribution details). Used here under the MIT license of midi-js-soundfonts.

Usage:
    python scripts/fetch_soundfonts.py          # download into cache dir
    python scripts/fetch_soundfonts.py --check  # only verify what's present

Caching: files are content-verified by size; existing valid files are skipped,
so the script is idempotent and re-runs are nearly free.
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

# abcjs spellings for black keys are flat-names (Bb/Db/Eb/Gb/Ab) — see its
# pitch-to-note-name table; sharp names are never requested.
NOTES = [
    "A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7",
    "Ab1", "Ab2", "Ab3", "Ab4", "Ab5", "Ab6", "Ab7",
    "B0", "B1", "B2", "B3", "B4", "B5", "B6", "B7",
    "Bb0", "Bb1", "Bb2", "Bb3", "Bb4", "Bb5", "Bb6", "Bb7",
    "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8",
    "Db1", "Db2", "Db3", "Db4", "Db5", "Db6", "Db7", "Db8",
    "D1", "D2", "D3", "D4", "D5", "D6", "D7",
    "Eb1", "Eb2", "Eb3", "Eb4", "Eb5", "Eb6", "Eb7",
    "E1", "E2", "E3", "E4", "E5", "E6", "E7",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7",
    "Gb1", "Gb2", "Gb3", "Gb4", "Gb5", "Gb6", "Gb7",
    "G1", "G2", "G3", "G4", "G5", "G6", "G7",
]
BASE = "https://paulrosen.github.io/midi-js-soundfonts/FluidR3_GM/acoustic_grand_piano-mp3"
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "app" / "static" / "soundfonts" / "FluidR3_GM" / "acoustic_grand_piano-mp3"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify presence only, no downloads")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    missing, empty = [], []
    for note in NOTES:
        f = CACHE / f"{note}.mp3"
        if not f.exists():
            missing.append(note)
        elif f.stat().st_size == 0:
            empty.append(note)

    todo = missing + empty
    if args.check:
        print(f"{len(NOTES) - len(todo)}/{len(NOTES)} samples present, {len(todo)} missing")
        return 0 if not todo else 1
    if not todo:
        print(f"Cache complete: {len(NOTES)}/{len(NOTES)} samples in {CACHE}")
        return 0

    print(f"Downloading {len(todo)} sample(s) from midi-js-soundfonts …")
    failed = []
    for note in todo:
        f = CACHE / f"{note}.mp3"
        url = f"{BASE}/{note}.mp3"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp, open(f, "wb") as out:
                out.write(resp.read())
            print(f"  ✓ {note}.mp3 ({f.stat().st_size} bytes)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {note}: {exc}", file=sys.stderr)
            failed.append(note)

    if failed:
        print(f"\n{len(failed)} failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\nDone: {len(NOTES)}/{len(NOTES)} samples cached in {CACHE}")
    print("License: midi-js-soundfonts (MIT) — https://github.com/paulrosen/midi-js-soundfonts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
