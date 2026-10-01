"""Regression tests for app/abc_native.py — the YuE2 native ABC validator.

Parity with the yue2-music skill reference implementation was verified during
the port (parse/strip_chords/instrumentalize outputs identical); these tests
pin that behavior without importing the skill bundle.

Run: uv run python -m tests.test_abc_native
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import abc_native as a  # noqa: E402

SAMPLE = '''X:1
T:
M:4/4
L:1/32
Q:1/4=88
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:G
% verse
V: Vocal
"Gmaj7"B8d8"Am7"c8A8|"D7"F16"G"G16|
V: Ins
Z2|
% chorus
V: Vocal
"C"e8g8"Am"a8g8|c16z16|
V: Ins
B8d8c8B8|d16e16|
'''


def _solo_bar(body: str, key: str = "C") -> a.Score:
    text = SAMPLE.replace('K:G', f'K:{key}')
    # swap the verse vocal bar for the test bar, silence everything else
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.startswith('"Gmaj7"'):
            lines[i] = body + '|'
        elif i in (10, 12, 15, 17):  # music lines
            lines[i] = 'Z|' if lines[i - 1].startswith('V: Ins') else 'Z|'
    return a.parse_abc('\n'.join(lines) + '\n')


def test_parse_basic():
    s = a.parse_abc(SAMPLE)
    assert s.bpm == 88
    assert len(s.voices["Vocal"].notes) == 11
    assert len(s.voices["Ins"].notes) == 6
    assert len(s.voices["Vocal"].chords) == 6
    assert not s.voices["Ins"].chords
    assert len(s.voices["Vocal"].bars) == len(s.voices["Ins"].bars) == 4


def test_key_signature_sounding_pitch():
    # K:G — unmarked F sounds F#(66); =F forces natural(65)
    s = _solo_bar("F32", key="G")
    assert s.voices["Vocal"].notes[0][1] == 66
    s = _solo_bar("=F32", key="G")
    assert s.voices["Vocal"].notes[0][1] == 65


def test_accidental_propagation_cross_octave():
    # Native rule: ^F propagates by letter across octaves within the bar;
    # resets at the barline.
    text = SAMPLE.replace('K:G', 'K:C')
    lines = text.splitlines()
    lines[10] = '^F16f16|'      # verse Vocal: 2 notes, 1 bar
    lines[12] = 'Z|'            # verse Ins: 1 bar
    lines[15] = 'F32|'          # chorus Vocal: 1 bar
    lines[17] = 'Z|'            # chorus Ins: 1 bar
    s = a.parse_abc('\n'.join(lines) + '\n')
    pitches = [n[1] for n in s.voices["Vocal"].notes]
    assert pitches == [66, 78, 65], pitches


def test_strip_chords_preserves_music():
    out = a.strip_chords(SAMPLE)
    before, after = a.parse_abc(SAMPLE), a.parse_abc(out)
    cmp = a.compare(before, after)
    assert cmp["match"], cmp
    assert not any(v.chords for v in after.voices.values())


def test_strip_chords_removes_only_chords():
    out = a.strip_chords(SAMPLE)
    # the vocal music lines no longer contain quoted annotations
    after = a.parse_abc(out)
    for idx in after.music_lines:
        assert '"' not in out.splitlines()[idx]


def test_instrumentalize_moves_vocal_to_ins():
    conv, report = a.instrumentalize(SAMPLE)
    vs = a.parse_abc(conv)
    assert not vs.voices["Vocal"].notes
    assert len(vs.voices["Ins"].notes) == report["output_ins_notes"] == 12
    assert report["vocal_notes_before"] == 11
    assert report["meter_and_tempo_preserved"]
    assert report["chords"] == "preserved"
    # every original vocal note preserved exactly (pitch+onset+duration merged)
    for note in a.parse_abc(SAMPLE).voices["Vocal"].notes:
        assert note in vs.voices["Ins"].notes


def test_instrumentalize_rejects_bad_overlap_mode():
    try:
        a.instrumentalize(SAMPLE, overlap="nope")
        assert False
    except a.AbcError:
        pass


def test_validate_instrumental_score():
    conv, _ = a.instrumentalize(SAMPLE)
    a.validate_instrumental_score(conv)  # passes
    try:
        a.validate_instrumental_score(SAMPLE)
        assert False
    except a.AbcError:
        pass


def test_instrumental_style_and_lyrics():
    st = a.instrumental_style("energetic eurodance, 128 bpm")
    assert st.lower().startswith("instrumental")
    for cond in ("no vocals", "no singing", "no choir", "no spoken words"):
        assert cond in st.lower()
    ly = a.instrumental_lyrics(SAMPLE)
    assert ly == "[Verse]\n\n[Chorus]\n"


def test_quick_inspect():
    qi = a.quick_inspect(SAMPLE)
    assert qi["valid"] and qi["has_chords"] and qi["bpm"] == 88
    assert qi["vocal_notes"] == 11 and qi["ins_notes"] == 6
    assert qi["meter"] == "4/4" and qi["key"] == "G"
    bad = a.quick_inspect("not abc")
    assert not bad["valid"] and bad["error"]


def test_fail_closed():
    for bad in (
        "X:1\nT:\nM:4/4",                                   # truncated
        SAMPLE.replace("M:4/4\n", "M:4/4\nM:3/4\n"),        # duplicate-ish structure
        SAMPLE.replace("B8d8", "B9d7"),                     # unsupported duration 9
        SAMPLE.replace('"Gmaj7"', '"Gmaj9"'),               # unsupported chord quality
        SAMPLE.replace("|", "||", 1),                       # double barline
    ):
        try:
            a.parse_abc(bad)
            assert False, f"should fail: {bad[:40]!r}"
        except a.AbcError:
            pass


def test_compile_events_roundtrip():
    # compile_events is the INSTRUMENTAL compiler: notes land in Ins,
    # Vocal carries only chords/rests (same voice mapping instrumentalize uses).
    src = a.parse_abc(SAMPLE)
    events = dict(
        bpm=src.bpm,
        key=src.voices["Vocal"].keys[0][1],
        bars=[dict(meter=f"{n}/{d}", key=next(k for t, k in reversed(src.voices['Vocal'].keys) if t <= start))
              for start, _, (n, d) in src.voices["Vocal"].bars],
        notes=[[str(t), str(d), p] for t, p, d in src.voices["Vocal"].notes],
        chords=[[str(w), s] for w, s in src.voices["Vocal"].chords],
    )
    text, check = a.compile_events(events)
    assert check["event_roundtrip"]
    reparsed = a.parse_abc(text)
    assert not reparsed.voices["Vocal"].notes
    assert [tuple(x) for x in reparsed.voices["Ins"].notes] == \
        [tuple(x) for x in src.voices["Vocal"].notes]
    # chord timeline survives (dedup'd against bar-start repeats)
    def dedup(evs):
        out = []
        for item in evs:
            if not out or out[-1][1] != item[1]:
                out.append(tuple(item))
        return out
    assert dedup(reparsed.voices["Vocal"].chords) == dedup(src.voices["Vocal"].chords)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  ✗ {name}: {exc}")
    print("FAILED" if failures else "ALL PASSED")
    sys.exit(1 if failures else 0)
