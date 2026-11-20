"""The demo pipeline (docs/demo/README.md): the script as data, captions, chapters, the
frame list, the ledger copy and the cards. No TTS, browser or ffmpeg here."""

import json
from pathlib import Path

import pytest

from meridian.demo import cards, pipeline
from meridian.demo.script import EM_DASH, ScriptError, load, parse, spoken
from meridian.demo.tts import SceneAudio, Timed
from meridian.platform.clock import FixedClock
from tests.support import T0, make_ledger

SCRIPT = """# Demo

## Scene 1: One (about 10 s)

**Narration:**
> The NOAEL is set. A person decides
> with seq 5.

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "A person decides" | `x` | Who decides |

**Sources:** none.

## Scene 2: Two (about 5 s)

**Narration:**
> Done.

**Actions:** none.
"""


def test_the_committed_script_parses_into_ten_scenes_with_every_cue_placed() -> None:
    scenes = load()
    assert [s.number for s in scenes] == list(range(1, 11))
    assert all(s.sentences for s in scenes)
    assert all(0 <= c.sentence < len(s.sentences) for s in scenes for c in s.cues)
    assert EM_DASH not in Path("docs/demo/script.md").read_text(encoding="utf-8")


def test_a_scene_splits_into_sentences_and_cues_name_theirs() -> None:
    one, two = parse(SCRIPT)
    assert one.sentences == ("The NOAEL is set.", "A person decides with seq 5.")
    assert (one.cues[0].sentence, one.cues[0].chip) == (1, "Who decides")
    assert two.sentences == ("Done.",)


def test_a_cue_naming_no_sentence_or_an_em_dash_is_refused() -> None:
    with pytest.raises(ScriptError, match="names no sentence"):
        parse(SCRIPT.replace('"A person decides"', '"nobody says this"'))
    with pytest.raises(ScriptError, match="em dash"):
        parse(SCRIPT + EM_DASH)


def test_spoken_forms_change_the_audio_text_only() -> None:
    assert spoken("The NOAEL and RBQM, seq 20068.") == "The Noel and R B Q M, sequence 20068."


def _plan() -> dict[str, object]:
    scenes = parse(SCRIPT)
    audio = [
        SceneAudio(1, Path("a.wav"), 10.0, (Timed("The NOAEL is set.", 0.6, 2.0),
                                             Timed("A person decides with seq 5.", 2.5, 9.0))),
        SceneAudio(2, Path("b.wav"), 4.0, (Timed("Done.", 0.6, 1.5),)),
    ]  # fmt: skip
    return pipeline.timeline(scenes, audio)


def test_captions_and_transcript_follow_the_measured_audio() -> None:
    plan = _plan()
    vtt = pipeline.captions(plan)
    assert vtt.startswith("WEBVTT\n")
    assert "00:00:02.500 --> 00:00:09.000\nA person decides with seq 5." in vtt
    assert "00:00:10.600 --> 00:00:11.500\nDone." in vtt  # scene 2 starts at 10.0
    t = pipeline.transcript(plan)
    assert t["duration"] == 14.0
    assert [c["start"] for c in t["chapters"]] == [0.0, 10.0]
    assert t["segments"][2] == {"id": "s02-00", "scene": 2, "chapter": "Two", "start": 10.6,
                                "end": 11.5, "text": "Done."}  # fmt: skip


def test_chapters_and_frames_for_ffmpeg() -> None:
    meta = pipeline.chapters_metadata(_plan())
    assert "START=10000\nEND=14000\ntitle=2. Two" in meta
    concat = pipeline.ffconcat([{"png": "f1.png", "t": 0}, {"png": "f2.png", "t": 3.5}], 14.0,
                               Path("/x"))  # fmt: skip
    assert "duration 3.500" in concat
    assert "duration 10.500" in concat
    with pytest.raises(pipeline.DemoError, match="t = 0"):
        pipeline.ffconcat([{"png": "f1.png", "t": 1}], 2.0, Path("/x"))


def test_the_ledger_copy_is_verified_and_matches_the_source(tmp_path: Path) -> None:
    from meridian.data.datacut import record_data_cut

    source = tmp_path / "real.sqlite3"
    with make_ledger(source, FixedClock(T0)) as ledger:
        for cut in ("DC-A", "DC-B"):
            record_data_cut(ledger, clock=FixedClock(T0), actor="system.ingest",
                            study_id="MRD-201", cut_id=cut, path=Path(cut))  # fmt: skip
    before = source.read_bytes()
    report = pipeline.copy_ledger(source, tmp_path / "demo" / "copy.sqlite3")
    assert (report.events, report.last_seq, report.verified) == (2, 2, True)
    assert report.last_hash == report.source_hash_at_seq
    assert source.read_bytes() == before  # the source is opened read-only


def test_cards_carry_the_credit_and_no_em_dash(tmp_path: Path) -> None:
    leak = tmp_path / "kind-x.json"
    leak.write_text(json.dumps({"zones": ["rbqm"], "runs": [{}, {}], "passed": True, "sources": [
        {"source": "node", "canary_hits": [], "control_found": True,
         "positive_control": "a marker"}]}))  # fmt: skip
    stats = {"computed_at_seq": 9, "verification": {"events_checked": 9},
             "runs": {"completed": 3, "by_executor": {"substrate (kind)": 3},
                      "by_cut_scope": {"evaluation": 3}}}  # fmt: skip
    paths = cards.write_all(tmp_path / "cards", leak, stats, Path("docs/phase4b-summary.md"))
    texts = {p.name: p.read_text() for p in paths}
    assert "LibriTTS (CC BY 4.0)" in texts["closing.html"]
    assert "derived from the CDISC SDTM/ADaM Pilot Project data" in texts["closing.html"]
    assert "910 runs in 270 s" in texts["scale.html"]
    assert 'class="control yes"' in texts["leakcheck.html"]
    assert all(EM_DASH not in t for t in texts.values())
