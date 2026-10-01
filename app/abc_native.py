"""Native two-voice ABC dialect tools for YuE2 — ported from the yue2-music skill.

This is the *authoritative* validator for anything fed to YuE2 as `abc=`.
abcjs remains a UI preview only; it is NOT the source of truth (different
accidental-propagation semantics, lenient meter handling, etc).

Ported (algorithm-preserving, MIT/Apache-2.0 per skill LICENSE):
- parse/inspect/compare from scripts/abc_tools.py
- strip_chords (token-level removal + invariant verification)
- compile_events / spelling from instrumental/scripts/compile_score.py
- instrumentalize (Vocal → Ins event-level transfer) from instrumentalize.py

Fail-closed: unsupported notation raises AbcError instead of guessing timing.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from fractions import Fraction
from math import lcm

VOICES = ("Vocal", "Ins")
DURATIONS = {1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48}
QUALITIES = ("", "m", "dim", "aug", "7", "maj7", "m7", "dim7", "m7b5",
             "sus4", "sus2", "6", "m6", "7sus4", "m(maj7)")
PITCH_NAME = r"[A-G](?:bb|##|b|#)?"
CHORD = re.compile(PITCH_NAME + "(?:" + "|".join(re.escape(q) for q in QUALITIES)
                   + ")(?:/" + PITCH_NAME + ")?")
TOKEN = re.compile(
    r'"(?P<chord>[^"\n]*)"|\[K:(?P<key>[^\]\n]+)\]|'
    r"(?P<acc>\^\^|__|\^|_|=)?(?P<note>[A-Ga-gz])"
    r"(?P<oct>[,']*)(?P<duration>[0-9]*)(?P<tie>-?)"
)
NATURAL = dict(zip("CDEFGAB", (0, 2, 4, 5, 7, 9, 11)))
KEYS = {
    **dict(zip(("Cb", "Gb", "Db", "Ab", "Eb", "Bb", "F", "C", "G", "D", "A", "E", "B", "F#", "C#"), range(-7, 8))),
    **dict(zip(("Abm", "Ebm", "Bbm", "Fm", "Cm", "Gm", "Dm", "Am", "Em", "Bm", "F#m", "C#m", "G#m", "D#m", "A#m"), range(-7, 8))),
}
SECTIONS = {'intro', 'verse', 'pre-chorus', 'chorus', 'bridge', 'interlude', 'outro', 'instrumental'}


class AbcError(ValueError):
    """Unsupported notation or a failed structural invariant.

    `location` (when available): {line_no (1-based), line_text} of the music line
    being parsed, so editors can highlight exactly where the score broke."""

    def __init__(self, message, location=None):
        super().__init__(message)
        self.location = location


_LOC = {}   # parse-time context: {line_no, line_text} of the music line being parsed


def fail(condition: bool, message: str) -> None:
    if condition:
        raise AbcError(message, dict(_LOC) if _LOC else None)


def key_accidentals(key: str) -> dict[str, int]:
    fail(key not in KEYS, f"Unsupported key {key!r}; use a standard major or minor K: field")
    sharps = KEYS[key]
    order_sharp = "FCGDAEB"
    result = {letter: 0 for letter in "ABCDEFG"}
    if sharps >= 0:
        for i in range(sharps):
            result[order_sharp[i]] = 1
    else:
        order_flat = "BEADGCF"
        for i in range(-sharps):
            result[order_flat[i]] = -1
    return result


def meter_value(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", text)
    fail(match is None, f"Unsupported meter {text!r}")
    n, d = int(match.group(1)), int(match.group(2))
    fail(n < 1 or d < 1 or (n, d) not in ((n, d),) or d & (d - 1) != 0 or d > 32 or n > 32,
         f"Unsupported meter {text!r}; use simple meters like 4/4, 3/4, 6/8, 7/8")
    return n, d


@dataclass
class Voice:
    meter: tuple[int, int]
    key: str
    keys: list = field(default_factory=list)
    notes: list = field(default_factory=list)    # [onset_quarters, midi_pitch, duration_quarters]
    chords: list = field(default_factory=list)   # [(onset_quarters, symbol)]
    bars: list = field(default_factory=list)     # [(start, length, (n, d))]
    time: Fraction = Fraction(0)
    pending: tuple | None = None


@dataclass
class Score:
    text: str
    unit: Fraction
    bpm: int
    voices: dict
    music_lines: dict


def parse_bar(body: str, voice: Voice, unit: Fraction, context: str) -> None:
    # Track the absolute bar number for error locations.
    m = re.search(r"bar (\d+)$", context)
    if m and _LOC:
        _LOC["bar_no"] = int(m.group(1))
    n, d = voice.meter
    length = Fraction(4 * n, d)
    start = voice.time
    offset = Fraction(0)
    local = {}  # Native exporters propagate accidentals by letter, across octaves.
    if body == "Z":
        fail(voice.pending is not None, f"{context}: tie enters a full-measure rest")
        offset = length
    else:
        cursor = 0
        while cursor < len(body):
            if body[cursor].isspace():
                cursor += 1
                continue
            match = TOKEN.match(body, cursor)
            fail(match is None, f"{context}: unsupported token at {body[cursor:cursor + 24]!r}")
            cursor = match.end()
            chord, key = match.group("chord", "key")
            fail(offset >= length, f"{context}: event after the measure end")
            if chord is not None:
                fail(CHORD.fullmatch(chord) is None, f"{context}: unsupported chord {chord!r}")
                voice.chords.append((start + offset, chord))
                continue
            if key is not None:
                key_accidentals(key)
                voice.key = key
                voice.keys.append((start + offset, key))
                local = {}
                continue
            note, acc, octave, tie = match.group("note", "acc", "oct", "tie")
            units = int(match.group("duration") or "1")
            fail(units not in DURATIONS, f"{context}: unsupported duration {units}; split it into tied supported lengths")
            duration = units * unit * 4
            fail(offset + duration > length, f"{context}: note/rest exceeds meter duration")
            fail("," in octave and "'" in octave, f"{context}: mixed octave marks")
            if note == "z":
                fail(bool(acc or octave or tie), f"{context}: a rest cannot have accidentals, octave marks or ties")
                fail(voice.pending is not None, f"{context}: tie enters a rest")
            else:
                letter = note.upper()
                written = 60 + NATURAL[letter] + (12 if note.islower() else 0)
                written += 12 * (octave.count("'") - octave.count(","))
                alteration = local.get(letter, key_accidentals(voice.key)[letter])
                if acc:
                    alteration = {"=": 0, "_": -1, "__": -2, "b": -1, "bb": -2,
                                  "^": 1, "^^": 2, "#": 1, "##": 2}[acc]
                    local[letter] = alteration
                pitch = written + alteration
                if voice.pending is not None:
                    old_pitch, old_written = voice.pending
                    # An unmarked continuation retains its tied accidental across
                    # a barline. It does not alter later untied notes in that bar.
                    if not acc and written == old_written:
                        pitch = old_pitch
                    fail(pitch != old_pitch, f"{context}: tie changes pitch from {old_pitch} to {pitch}")
                    voice.notes[-1][2] += duration
                else:
                    fail(not 0 <= pitch <= 127, f"{context}: pitch {pitch} is outside MIDI range")
                    voice.notes.append([start + offset, pitch, duration])
                voice.pending = (pitch, written) if tie else None
            offset += duration
    fail(offset != length, f"{context}: duration {offset} quarter notes != meter duration {length}")
    voice.bars.append((start, length, voice.meter))
    voice.time += length


def parse(text: str) -> Score:
    """Fail closed for unsupported tokens; resolve sounding notes, not token counts."""
    lines = text.splitlines()
    fail(len(lines) < 12, "Incomplete native two-voice ABC")
    fail(lines[0:2] != ["X:1", "T:"], "Expected native X:1 and blank T: header")
    fail(not lines[2].startswith("M:"), "Missing header M:")
    meter = meter_value(lines[2][2:])
    unit_match = re.fullmatch(r"L:1/([1-9][0-9]*)", lines[3])
    fail(unit_match is None, "Expected L:1/<power of two>, usually L:1/32")
    denominator = int(unit_match.group(1))
    fail(denominator > 1024 or denominator & (denominator - 1) != 0, "Unsupported L: denominator")
    unit = Fraction(1, denominator)
    tempo_match = re.fullmatch(r"Q:1/4=([1-9][0-9]*)", lines[4])
    fail(tempo_match is None, "Expected integer quarter-note tempo Q:1/4=<BPM>")
    expected_voices = ['V: Vocal clef=treble name="Vocal Melody" snm="Vocal"',
                       'V: Ins clef=treble name="Ins Melody" snm="Inst."']
    fail(lines[5:7] != expected_voices, "Preserve native Vocal and Ins voice definitions")
    fail(not lines[7].startswith("K:"), "Missing header K:")
    key = lines[7][2:]
    key_accidentals(key)
    voices = {name: Voice(meter, key, keys=[(Fraction(0), key)]) for name in VOICES}
    music_lines = {}
    cursor = 8
    group = 0
    while cursor < len(lines):
        while cursor < len(lines) and lines[cursor].startswith("% "):
            cursor += 1
        # structural errors carry the offending line's position too
        _LOC.clear()
        _LOC.update(line_no=cursor + 1, line_text=lines[cursor] if cursor < len(lines) else "")
        fail(cursor == len(lines), "Dangling section comment without music")
        group += 1
        counts = []
        for name in VOICES:
            context = f"group {group}, {name}"
            _LOC["voice"] = name
            _LOC["line_no"] = min(cursor, len(lines) - 1) + 1
            _LOC["line_text"] = lines[min(cursor, len(lines) - 1)]
            fail(cursor >= len(lines) or lines[cursor] != f"V: {name}", f"{context}: expected V: {name}")
            cursor += 1
            voice = voices[name]
            fields = set()
            while cursor < len(lines) and lines[cursor].startswith(("M:", "K:")):
                field_name, value = lines[cursor].split(":", 1)
                fail(field_name in fields, f"{context}: duplicate {field_name}: field")
                fields.add(field_name)
                if field_name == "M":
                    voice.meter = meter_value(value)
                else:
                    key_accidentals(value)
                    voice.key = value
                    voice.keys.append((voice.time, value))
                cursor += 1
            fail(cursor >= len(lines), f"{context}: missing music line")
            line = lines[cursor]
            fail(not line.endswith("|"), f"{context}: music line must end with a plain barline")
            music_lines[cursor] = name
            # Location context for every bar parsed from this line: 1-based line
            # number plus the raw text, so errors can be highlighted in the editor.
            _LOC.clear()
            _LOC.update(line_no=cursor + 1, line_text=line, voice=name)
            cursor += 1
            bars = []
            for bar in line[:-1].split("|"):
                bar = bar.strip()
                fail(not bar, f"{context}: empty measure or unsupported double/repeat barline")
                rest = re.fullmatch(r"Z([2-4])?", bar)
                if rest:
                    bars.extend(["Z"] * int(rest.group(1) or 1))
                else:
                    bars.append(bar)
            fail(not 1 <= len(bars) <= 4, f"{context}: expected 1–4 measures after expanding Z rests")
            counts.append(len(bars))
            for bar in bars:
                parse_bar(bar, voice, unit, f"{context}, bar {len(voice.bars) + 1}")
            _LOC.clear()
        fail(counts[0] != counts[1], f"group {group}: voices have different measure counts")
    for name, voice in voices.items():
        fail(voice.pending is not None, f"{name}: unresolved tie at end of score")
    fail(voices["Ins"].chords != [], "Native chord symbols belong in Vocal, not Ins")
    fail(voices["Vocal"].bars != voices["Ins"].bars, "Voice meter/time grids differ")
    fail(voices["Vocal"].keys != voices["Ins"].keys, "Voice key-change timelines differ")
    return Score(text, unit, int(tempo_match.group(1)), voices, music_lines)


def parse_abc(text: str) -> Score:
    """Public entry point; no model load, files, or optional dependencies."""
    return parse(text)


def report(score: Score) -> dict:
    return {
        "scope": "native-dialect structural check; not native serializer reconstruction or audio verification",
        "sha256": hashlib.sha256(score.text.encode()).hexdigest(),
        "bpm": score.bpm, "unit_length": str(score.unit),
        "duration_quarters": str(score.voices["Vocal"].time),
        "nominal_duration_seconds": float(score.voices["Vocal"].time * 60 / score.bpm),
        "voices": {name: {"sounding_notes": len(v.notes), "measures": len(v.bars),
                           "notes": [{"onset_quarters": str(t), "midi_pitch": p, "duration_quarters": str(d)}
                                     for t, p, d in v.notes],
                           "chords": [(str(a), b) for a, b in v.chords], "keys": v.keys, "bars": v.bars}
                   for name, v in score.voices.items()},
    }


def compare(before: Score, after: Score, names=VOICES, allow_tempo_change=False) -> dict:
    differences = []
    if before.bpm != after.bpm and not allow_tempo_change:
        differences.append("quarter-note tempo differs")
    for name in names:
        a, b = before.voices[name], after.voices[name]
        if a.bars != b.bars:
            differences.append(f"{name}: bar meter/time grid differs")
        if a.notes != b.notes:
            common = min(len(a.notes), len(b.notes))
            first = next((i for i in range(common) if a.notes[i] != b.notes[i]), common)
            differences.append(f"{name}: sounding notes differ starting at note {first + 1} (pitch, onset or duration)")
    return {"match": not differences, "compared_voices": list(names),
            "tempo_change_allowed": allow_tempo_change, "differences": differences,
            "scope": "exact symbolic melody and meter; no claim about generated audio or unchanged harmony"}


def strip_chords(text: str, keep_voice="both") -> str:
    """Remove only supported quoted chord symbols from music lines; verify all
    sounding notes, onsets, durations, meters and tempo remain unchanged."""
    source = parse(text)
    lines = text.splitlines(keepends=True)
    for index, name in source.music_lines.items():
        def transform(match):
            if match.group("chord") is not None:
                return ""
            if keep_voice != "both" and name != keep_voice and match.group("note") is not None:
                return "z" + match.group("duration")
            return match.group(0)
        lines[index] = TOKEN.sub(transform, lines[index])
    output = "".join(lines)
    result = parse(output)
    names = VOICES if keep_voice == "both" else (keep_voice,)
    invariant = compare(source, result, names)
    fail(not invariant["match"], f"Chord removal changed melody: {invariant['differences']}")
    fail(any(v.chords for v in result.voices.values()), "Chord removal left a chord symbol")
    if keep_voice != "both":
        fail(any(v.notes for name, v in result.voices.items() if name != keep_voice), "Unselected voice was not silenced")
    return output


# ---------- compile_score port: events → native ABC (for instrumentalize) ----------

def _fraction(value, context):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AbcError(f"{context}: invalid fraction {value!r}")
    if isinstance(value, str):
        try:
            result = Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise AbcError(f"{context}: invalid fraction {value!r}") from exc
    else:
        result = Fraction(value)
    return result


def _lengths(value: Fraction):
    if value.denominator != 1 or value <= 0:
        raise AbcError(f"Duration {value} is outside the native rhythmic grid")
    result = []
    for unit in (48, 32, 24, 16, 12, 8, 6, 4, 3, 2, 1):
        while value >= unit:
            result.append(unit)
            value -= unit
    return result


def spelling(pitch, key, active):
    signature = key_accidentals(key)
    prefer_sharp = sum(signature.values()) >= 0
    candidates = []
    for letter, pc in NATURAL.items():
        for alteration in (-1, 0, 1):
            base = pitch - alteration - 60 - pc
            if base % 12:
                continue
            octave = base // 12
            score = (alteration != signature[letter], abs(alteration),
                     (alteration < 0) if prefer_sharp else (alteration > 0))
            candidates.append((score, letter, alteration, octave))
    _, letter, alteration, octave = min(candidates)
    accidental = ''
    if alteration != active.get(letter, signature[letter]):
        accidental = {-1: '_', 0: '=', 1: '^'}[alteration]
        active[letter] = alteration
    name = letter + ',' * (-octave) if octave < 0 else (
        letter if octave == 0 else letter.lower() + "'" * (octave - 1))
    return accidental + name


def _compress(bars):
    result, index = [], 0
    while index < len(bars):
        if bars[index] != 'Z':
            result.append(bars[index] + '|')
            index += 1
            continue
        end = index + 1
        while end < len(bars) and bars[end] == 'Z':
            end += 1
        count = end - index
        result.append('Z' + (str(count) if count > 1 else '') + '|')
        index = end
    return ''.join(result)


def compile_events(data):
    """Compile beat-domain events (bpm/key/bars/notes/chords) to native ABC."""
    if not isinstance(data, dict) or set(data) - {'bpm', 'key', 'bars', 'notes', 'chords'}:
        raise AbcError('Score requires bpm, key, bars, notes and optional chords; unknown fields are rejected')
    bpm = data.get('bpm')
    if type(bpm) is not int or bpm <= 0:
        raise AbcError('bpm must be a positive integer quarter-note tempo')
    key = data.get('key', 'C')
    key_accidentals(key)
    if not isinstance(data.get('bars'), list) or not data['bars']:
        raise AbcError('bars must be a nonempty list')
    bars, time, meter, section = [], Fraction(0), '4/4', None
    unit_denominator = 32
    for i, item in enumerate(data['bars']):
        if not isinstance(item, dict) or set(item) - {'meter', 'key', 'section'}:
            raise AbcError(f'bar {i + 1}: only meter, key and section are supported')
        meter, key = item.get('meter', meter), item.get('key', key)
        n, d = meter_value(meter)
        key_accidentals(key)
        unit_denominator = lcm(unit_denominator, 4 * d)
        incoming = item.get('section', section)
        if incoming is not None and incoming not in SECTIONS:
            raise AbcError(f'bar {i + 1}: section must be one of {sorted(SECTIONS)}')
        section = incoming
        end = time + Fraction(4 * n, d)
        bars.append(dict(start=time, end=end, meter=meter, key=key, section=section))
        time = end
    notes = []
    for i, item in enumerate(data.get('notes', [])):
        if not isinstance(item, list) or len(item) != 3:
            raise AbcError(f'note {i + 1}: expected [onset, duration, MIDI pitch]')
        start, duration = _fraction(item[0], f'note {i + 1} onset'), _fraction(item[1], f'note {i + 1} duration')
        pitch = item[2]
        if type(pitch) is not int or not 0 <= pitch <= 127:
            raise AbcError(f'note {i + 1}: pitch must be one MIDI integer; reduce polyphony first')
        if start < 0 or duration <= 0 or start + duration > time:
            raise AbcError(f'note {i + 1}: onset/duration is outside the score')
        notes.append((start, pitch, duration))
        unit_denominator = lcm(unit_denominator, (start / 4).denominator, (duration / 4).denominator)
    notes.sort()
    if not notes:
        raise AbcError('Instrumental score has no sounding notes')
    for a, b in zip(notes, notes[1:]):
        if a[0] + a[2] > b[0]:
            raise AbcError(f'Overlapping melody notes at quarter {b[0]}; choose one lead line before compilation')
    chords = []
    for i, item in enumerate(data.get('chords', [])):
        if not isinstance(item, list) or len(item) != 2:
            raise AbcError(f'chord {i + 1}: expected [onset, symbol]')
        when = _fraction(item[0], f'chord {i + 1} onset')
        symbol = item[1]
        if when < 0 or when >= time or not isinstance(symbol, str) or not CHORD.fullmatch(symbol):
            raise AbcError(f'chord {i + 1}: unsupported symbol or onset')
        chords.append((when, symbol))
        unit_denominator = lcm(unit_denominator, (when / 4).denominator)
    chords.sort()
    if any(a[0] == b[0] for a, b in zip(chords, chords[1:])):
        raise AbcError('Conflicting/duplicate chord events at the same onset; choose one harmony timeline')
    if unit_denominator > 1024 or unit_denominator & (unit_denominator - 1):
        raise AbcError('Rhythm is outside the power-of-two training grid; explicitly quantize tuplets and record the changes')
    unit_quarters = Fraction(4, unit_denominator)
    ins, vocal = [], []
    for bar in bars:
        start, end = bar['start'], bar['end']
        events = [x for x in notes if x[0] < end and x[0] + x[2] > start]
        pieces, cursor, active = [], start, {}
        for onset, pitch, duration in events:
            a, b = max(start, onset), min(end, onset + duration)
            if a > cursor:
                pieces.extend('z' + (str(n) if n != 1 else '') for n in _lengths((a - cursor) / unit_quarters))
            parts = _lengths((b - a) / unit_quarters)
            for i, count in enumerate(parts):
                tie = '-' if i < len(parts) - 1 or b < onset + duration else ''
                pieces.append(spelling(pitch, bar['key'], active) + (str(count) if count != 1 else '') + tie)
            cursor = b
        if cursor < end:
            pieces.extend('z' + (str(n) if n != 1 else '') for n in _lengths((end - cursor) / unit_quarters))
        ins.append(''.join(pieces) if events else 'Z')
        current = next((symbol for when, symbol in reversed(chords) if when <= start), None)
        changes = [(start, current)] + [(when, symbol) for when, symbol in chords if start < when < end]
        chunks = []
        for i, (when, symbol) in enumerate(changes):
            stop = changes[i + 1][0] if i + 1 < len(changes) else end
            chunks.append(('"' + symbol + '"') if symbol else '')
            chunks.extend('z' + (str(n) if n != 1 else '') for n in _lengths((stop - when) / unit_quarters))
        vocal.append(''.join(chunks) if any(symbol for _, symbol in changes) else 'Z')
    header = ['X:1', 'T:', 'M:' + bars[0]['meter'], f'L:1/{unit_denominator}', f'Q:1/4={bpm}',
              'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"',
              'V: Ins clef=treble name="Ins Melody" snm="Inst."', 'K:' + bars[0]['key']]
    lines, index = header, 0
    while index < len(bars):
        bar = bars[index]
        previous = bars[index - 1] if index else bar
        end = index + 1
        while end < len(bars) and end - index < 4 and all(bars[end][k] == bar[k] for k in ('meter', 'key', 'section')):
            end += 1
        if bar['section'] is not None and (index == 0 or bar['section'] != previous['section']):
            lines.append('% ' + bar['section'])
        for voice, rendered in (('Vocal', vocal), ('Ins', ins)):
            lines.append('V: ' + voice)
            if bar['meter'] != previous['meter']:
                lines.append('M:' + bar['meter'])
            if bar['key'] != previous['key']:
                lines.append('K:' + bar['key'])
            lines.append(_compress(rendered[index:end]))
        index = end
    text = '\n'.join(lines) + '\n'
    parsed = parse_abc(text)
    if [tuple(x) for x in parsed.voices['Ins'].notes] != notes or parsed.voices['Vocal'].notes:
        raise AbcError('Internal note roundtrip failed')
    def dedup(events):
        result = []
        for item in events:
            if not result or result[-1][1] != item[1]:
                result.append(tuple(item))
        return result
    if dedup(parsed.voices['Vocal'].chords) != dedup(chords):
        raise AbcError('Internal harmony roundtrip failed')
    check = report(parsed)
    check['event_roundtrip'] = True
    return text, check


# ---------- instrumentalize port: Vocal → Ins event-level transfer ----------

def _section_starts(text, score):
    """Map native section comments to their absolute bar-start times."""
    cursor, pending = 0, None
    result = {}
    for index, line in enumerate(text.splitlines()):
        if line.startswith('% '):
            pending = line[2:]
            if pending not in SECTIONS:
                raise AbcError(f'Unknown section label: {pending}')
        if score.music_lines.get(index) != 'Vocal':
            continue
        if pending is not None:
            result[score.voices['Vocal'].bars[cursor][0]] = pending
            pending = None
        for bar in line[:-1].split('|'):
            rest = re.fullmatch(r'Z([2-4])?', bar.strip())
            cursor += int(rest.group(1) or 1) if rest else 1
    return result


def _subtract_intervals(start, duration, occupied):
    pieces = [(start, start + duration)]
    for left, right in occupied:
        remaining = []
        for a, b in pieces:
            if right <= a or left >= b:
                remaining.append((a, b))
            else:
                if a < left:
                    remaining.append((a, left))
                if right < b:
                    remaining.append((right, b))
        pieces = remaining
    return pieces


def instrumentalize(text, *, overlap='vocal', keep_chords=True):
    """Move every Vocal sounding note to Ins (event-level, not string replace).

    Vocal keeps only rests/chords. Original non-conflicting Ins melody is
    preserved; on Vocal/Ins overlap the Vocal wins and clipped Ins segments
    are recorded. Returns (instrumental_abc, transfer_report).
    """
    if overlap not in ('vocal', 'error'):
        raise AbcError('overlap must be vocal or error')
    if not isinstance(text, str) or not text.strip():
        raise AbcError('Need a nonempty native score')
    source = parse(text)
    vocal, ins = source.voices['Vocal'], source.voices['Ins']
    if not vocal.notes and not ins.notes:
        raise AbcError('The score contains no sounding notes')
    occupied = [(t, t + d) for t, _, d in vocal.notes]
    merged = [list(n) for n in vocal.notes]
    affected = []
    unchanged = 0
    for index, (start, pitch, duration) in enumerate(ins.notes):
        pieces = _subtract_intervals(start, duration, occupied)
        if pieces == [(start, start + duration)]:
            unchanged += 1
        else:
            if overlap == 'error':
                raise AbcError('Vocal and Ins overlap; choose vocal priority or edit the arrangement explicitly')
            affected.append(dict(ins_note=index, onset=str(start), pitch=pitch, duration=str(duration),
                                 retained_segments=[[str(a), str(b - a)] for a, b in pieces]))
        merged.extend([a, pitch, b - a] for a, b in pieces)
    merged.sort()
    sections = _section_starts(text, source)
    starts = {start for start, _, _ in vocal.bars}
    if any(when not in starts for when, _ in vocal.keys):
        raise AbcError('An inline key change occurs inside a bar; normalize its spelling without changing pitches first')
    bars = []
    for start, _, (n, d) in vocal.bars:
        key = next(key for t, key in reversed(vocal.keys) if t <= start)
        bar = dict(meter=f'{n}/{d}', key=key)
        if start in sections:
            bar['section'] = sections[start]
        bars.append(bar)
    chords = []
    if keep_chords:
        for when, symbol in vocal.chords:
            chords.append([str(when), symbol])
    events = dict(bpm=source.bpm, key=vocal.keys[0][1], bars=bars,
                  notes=[[str(t), str(d), p] for t, p, d in merged], chords=chords)
    converted, _check = compile_events(events)
    target = parse(converted)
    if target.voices['Ins'].notes != merged or target.voices['Vocal'].notes:
        raise AbcError('Voice transfer changed the intended note events')
    if target.voices['Ins'].bars != ins.bars or target.bpm != source.bpm:
        raise AbcError('Voice transfer changed meter, timing or tempo')
    for note in vocal.notes:
        if note not in target.voices['Ins'].notes:
            raise AbcError('A vocal note was not preserved exactly')
    def harmony(chord_list):
        result = []
        for event in chord_list:
            if not result or result[-1][1] != event[1]:
                result.append(event)
        return result
    if keep_chords and harmony(target.voices['Vocal'].chords) != harmony(vocal.chords):
        raise AbcError('Voice transfer changed harmony')
    transfer = dict(vocal_notes_before=len(vocal.notes), vocal_notes_after=0,
                    original_ins_notes=len(ins.notes), unaltered_ins_notes=unchanged,
                    output_ins_notes=len(merged), vocal_pitch_onset_duration_preserved=True,
                    meter_and_tempo_preserved=True,
                    chords='preserved' if keep_chords else 'removed for melody cover',
                    overlap_policy=overlap, affected_ins_notes=affected,
                    nominal_seconds=float(ins.time * 60 / source.bpm))
    return converted, transfer


def validate_instrumental_score(text) -> Score:
    """Check that a score is already instrumental (Vocal rests-only)."""
    score = parse(text)
    if score.voices['Vocal'].notes:
        raise AbcError('Vocal contains sounding notes; move the intended lead to Ins first')
    if not score.voices['Ins'].notes:
        raise AbcError('Ins has no sounding notes')
    return score


def instrumental_style(style: str) -> str:
    """Ensure the style prompt carries the no-vocals conditions."""
    style = (style or 'Instrumental, warm lyrical acoustic piano').strip().rstrip('.,')
    if not re.match(r'^instrumental\b', style, re.I):
        style = 'Instrumental, ' + style
    for condition in ('no vocals', 'no singing', 'no choir', 'no spoken words'):
        if condition not in style.lower():
            style += ', ' + condition
    return style + '.'


def instrumental_lyrics(text: str) -> str:
    """Section-tag-only lyrics derived from the score's section comments."""
    labels = [line[2:] for line in text.splitlines() if line.startswith('% ')]
    return '\n\n'.join('[' + x.title() + ']' for x in labels) + ('\n' if labels else '')


def quick_inspect(text: str) -> dict:
    """Lightweight summary for UI badges: valid?, chords?, tempo, meter, notes."""
    try:
        score = parse(text)
        vocal, ins = score.voices['Vocal'], score.voices['Ins']
        return {"valid": True, "error": None,
                "has_chords": bool(vocal.chords),
                "chord_count": len(vocal.chords),
                "bpm": score.bpm,
                "meter": f"{vocal.bars[0][2][0]}/{vocal.bars[0][2][1]}" if vocal.bars else None,
                "key": vocal.keys[0][1] if vocal.keys else None,
                "vocal_notes": len(vocal.notes), "ins_notes": len(ins.notes),
                "measures": len(vocal.bars),
                "nominal_seconds": float(vocal.time * 60 / score.bpm)}
    except AbcError as exc:
        return {"valid": False, "error": str(exc), "location": exc.location}
