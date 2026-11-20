"""The demo recording pipeline (docs/demo/README.md), host side.

prepare: copy the ledger and verify the copy; parse the script; synthesise each scene's
narration; write the plan the recorder follows, the captions and the transcript; generate
the card pages. assemble: turn the recorder's frames and the narration into var/demo/demo.mp4
with chapters. Nothing here writes to the real ledger: it is opened read-only and copied
with SQLite's backup API.
"""

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from meridian.demo import cards, tts
from meridian.demo.script import EM_DASH, Scene, as_json, load
from meridian.domain.identity import operator_id
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.platform.clock import SystemClock
from meridian.platform.config import DEFAULT_FINDING_ID_PREFIXES

OUT = Path("var/demo")
RECORDER = operator_id("demo-recorder")
LEAK_FILE = Path("var/leakcheck/kind-20261119T202520Z.json")
PHASE4B = Path("docs/phase4b-summary.md")
FPS = 30


class DemoError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CopyReport:
    source: str
    copy: str
    events: int
    last_seq: int
    last_hash: str
    source_hash_at_seq: str
    verified: bool


def _last(path: Path, read_only: bool) -> tuple[int, str]:
    uri = f"file:{path}?mode=ro" if read_only else f"file:{path}"
    with sqlite3.connect(uri, uri=True) as c:
        row = c.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    if row is None:
        raise DemoError(f"{path}: an empty ledger")
    return int(row[0]), str(row[1])


def copy_ledger(source: Path, dest: Path) -> CopyReport:
    """The real ledger, opened read-only, copied with the backup API (a consistent snapshot
    even while it is open elsewhere); the copy verified and its last hash compared with the
    source's hash at the same seq."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{dest}{suffix}").unlink(missing_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    seq, last_hash = _last(dest, read_only=False)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as c:
        row = c.execute("SELECT hash FROM events WHERE seq = ?", (seq,)).fetchone()
    source_hash = str(row[0]) if row else ""
    with SqliteLedger(
        path=dest, clock=SystemClock(), finding_id_prefixes=DEFAULT_FINDING_ID_PREFIXES
    ) as ledger:
        report = verify(ledger)
    if not report.ok or source_hash != last_hash or report.events_checked != seq:
        raise DemoError(f"the copy does not match or verify: {report}, {source_hash}")
    return CopyReport(str(source), str(dest), report.events_checked, seq, last_hash,
                      source_hash, report.ok)  # fmt: skip


def _vtt_time(t: float) -> str:
    ms = round(t * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def timeline(scenes: Sequence[Scene], audio: Sequence[tts.SceneAudio]) -> dict[str, Any]:
    """Global times: each scene starts where the previous one's audio ends."""
    out: list[dict[str, Any]] = []
    t = 0.0
    for scene, a in zip(scenes, audio, strict=True):
        out.append(
            {
                "number": scene.number,
                "title": scene.title,
                "start": round(t, 3),
                "duration": a.duration,
                "sentences": [asdict(s) for s in a.sentences],
                "cues": [asdict(c) for c in scene.cues],
            }
        )
        t += a.duration
    return {"duration": round(t, 3), "fps": FPS, "scenes": out}


def captions(plan: dict[str, Any]) -> str:
    lines = ["WEBVTT", ""]
    n = 0
    for scene in plan["scenes"]:
        for s in scene["sentences"]:
            n += 1
            lines += [
                str(n),
                f"{_vtt_time(scene['start'] + s['start'])} --> "
                f"{_vtt_time(scene['start'] + s['end'])}",
                s["text"],
                "",
            ]
    text = "\n".join(lines)
    if EM_DASH in text:
        raise DemoError("an em dash in the captions")
    return text


def transcript(plan: dict[str, Any]) -> dict[str, Any]:
    segments = []
    for scene in plan["scenes"]:
        for i, s in enumerate(scene["sentences"]):
            segments.append(
                {
                    "id": f"s{scene['number']:02d}-{i:02d}",
                    "scene": scene["number"],
                    "chapter": scene["title"],
                    "start": round(scene["start"] + s["start"], 3),
                    "end": round(scene["start"] + s["end"], 3),
                    "text": s["text"],
                }
            )
    chapters = [
        {"scene": s["number"], "title": s["title"], "start": s["start"]} for s in plan["scenes"]
    ]
    return {"duration": plan["duration"], "chapters": chapters, "segments": segments}


def copy_stats(copy: Path, people_file: Path, operational: Sequence[str]) -> dict[str, Any]:
    """`meridian stats` on the copy (for the scale card)."""
    import tomllib

    from meridian import project_stats

    with people_file.open("rb") as handle:
        people = {pid: p["role"] for pid, p in tomllib.load(handle)["people"].items()}
    with SqliteLedger(
        path=copy, clock=SystemClock(), finding_id_prefixes=DEFAULT_FINDING_ID_PREFIXES
    ) as ledger:
        return project_stats.compute(
            ledger, operational_cuts=operational, people=people, var_dir=Path("var")
        )


def prepare(
    real_ledger: Path,
    piper: str,
    people_file: Path,
    operational: Sequence[str],
    out: Path = OUT,
) -> dict[str, Any]:
    if shutil.which(piper) is None:
        raise DemoError(f"no {piper} on PATH: uv tool install piper-tts==1.8.0")
    voice = tts.VOICE_FILE
    if not voice.exists():
        raise DemoError(f"no voice at {voice} (docs/demo/README.md)")
    report = copy_ledger(real_ledger, out / "ledger.sqlite3")
    scenes = load()
    voice_sha = tts.sha256_file(voice)
    audio = [
        tts.scene_audio(s, out / "audio", out / "audio" / "cache", piper, voice, voice_sha)
        for s in scenes
    ]
    tts.join(audio, out / "narration.wav")
    plan = timeline(scenes, audio)
    plan["script"] = as_json(scenes)
    plan["ledger_copy"] = asdict(report)
    plan["voice"] = tts.settings_key(voice_sha)
    # The identity of the recorder's one write attempt (refused): an operator, made like
    # every other operator id, never a person (meridian.domain.identity).
    plan["recorder_identity"] = RECORDER
    (out / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    (out / "captions.vtt").write_text(captions(plan), encoding="utf-8")
    (out / "transcript.json").write_text(
        json.dumps(transcript(plan), indent=2) + "\n", encoding="utf-8"
    )
    stats = copy_stats(out / "ledger.sqlite3", people_file, operational)
    (out / "stats.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    cards.write_all(out / "cards", LEAK_FILE, stats, PHASE4B)
    return plan


def ffconcat(frames: Sequence[dict[str, Any]], total: float, base: Path) -> str:
    """frames: [{"png": name, "t": seconds}] in time order across the whole video."""
    if not frames or frames[0]["t"] != 0:
        raise DemoError("the first frame must be at t = 0")
    lines = ["ffconcat version 1.0"]
    for i, f in enumerate(frames):
        end = frames[i + 1]["t"] if i + 1 < len(frames) else total
        if end < f["t"]:
            raise DemoError(f"frames out of order at {f}")
        lines += [f"file '{(base / f['png']).resolve()}'", f"duration {end - f['t']:.3f}"]
    lines.append(f"file '{(base / frames[-1]['png']).resolve()}'")  # the concat demuxer's last
    return "\n".join(lines) + "\n"


def chapters_metadata(plan: dict[str, Any]) -> str:
    lines = [";FFMETADATA1", "title=Meridian Agent Fleet v1.0, a narrated demo"]
    scenes = plan["scenes"]
    for i, s in enumerate(scenes):
        end = scenes[i + 1]["start"] if i + 1 < len(scenes) else plan["duration"]
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={round(s['start'] * 1000)}",
            f"END={round(end * 1000)}",
            f"title={s['number']}. {s['title']}",
        ]
    return "\n".join(lines) + "\n"


def assemble(ffmpeg: str = "ffmpeg", out: Path = OUT) -> Path:
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    frames = json.loads((out / "frames" / "frames.json").read_text(encoding="utf-8"))
    (out / "frames.ffconcat").write_text(
        ffconcat(frames["frames"], plan["duration"], out / "frames"), encoding="utf-8"
    )
    (out / "chapters.txt").write_text(chapters_metadata(plan), encoding="utf-8")
    video = out / "demo.mp4"
    subprocess.run(  # noqa: S603 (fixed argv: the operator's piper or ffmpeg)
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(out / "frames.ffconcat"),
            "-i", str(out / "narration.wav"),
            "-i", str(out / "chapters.txt"),
            "-map", "0:v", "-map", "1:a", "-map_metadata", "2", "-map_chapters", "2",
            "-vf", f"fps={FPS},format=yuv420p",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-threads", "1",
            "-c:a", "aac", "-b:a", "128k",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-movflags", "+faststart",
            "-t", f"{plan['duration']:.3f}",
            str(video),
        ],
        check=True,
    )  # fmt: skip
    return video
