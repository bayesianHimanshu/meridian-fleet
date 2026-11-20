"""Narration audio with Piper, a local TTS engine run as an external command (installed with
`uv tool install piper-tts==1.8.0`, outside this project's dependencies; docs/demo/README.md).

Each sentence is synthesised on its own, so every sentence's start and end in the scene is
known exactly: the captions, the transcript and the recorder's highlights use them. Noise is
off (noise_scale and noise_w 0), which makes Piper deterministic: the same text, voice and
settings give the same bytes. A sentence's WAV is cached under its hash.
"""

import hashlib
import json
import subprocess
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.demo.script import Scene, spoken

VOICE = "en_US-libritts-high"
VOICE_FILE = Path("var/demo/voice") / f"{VOICE}.onnx"
SPEAKER = 0
LENGTH_SCALE = 1.35  # slower than Piper's default; 1,208 words take 7 min 30 s, pauses included
LEAD_IN = 0.6  # seconds of silence before a scene's first sentence
GAP = 0.45  # between sentences
TAIL = 0.9  # after the last sentence
RATE = 22050


@dataclass(frozen=True, slots=True)
class Timed:
    text: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class SceneAudio:
    number: int
    wav: Path
    duration: float
    sentences: tuple[Timed, ...]


def settings_key(voice_sha256: str) -> dict[str, object]:
    return {
        "voice": VOICE,
        "voice_sha256": voice_sha256,
        "speaker": SPEAKER,
        "length_scale": LENGTH_SCALE,
        "noise_scale": 0,
        "noise_w": 0,
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def synthesise(text: str, out: Path, piper: str, voice: Path) -> None:
    subprocess.run(  # noqa: S603 (fixed argv: the operator's piper or ffmpeg)
        [
            piper,
            "-m",
            str(voice),
            "-s",
            str(SPEAKER),
            "--length-scale",
            str(LENGTH_SCALE),
            "--noise-scale",
            "0",
            "--noise-w-scale",
            "0",
            "-f",
            str(out),
        ],
        input=text,
        text=True,
        check=True,
        capture_output=True,
    )


def _frames(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"{path}: expected {RATE} Hz mono 16-bit")
        return w.readframes(w.getnframes())


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * round(seconds * RATE)


def scene_audio(
    scene: Scene, out_dir: Path, cache: Path, piper: str, voice: Path, voice_sha256: str
) -> SceneAudio:
    cache.mkdir(parents=True, exist_ok=True)
    key = json.dumps(settings_key(voice_sha256), sort_keys=True)
    pcm = bytearray(_silence(LEAD_IN))
    timed: list[Timed] = []
    for sentence in scene.sentences:
        say = spoken(sentence)
        name = hashlib.sha256((key + "\n" + say).encode()).hexdigest()[:24]
        cached = cache / f"{name}.wav"
        if not cached.exists():
            tmp = cached.with_suffix(".tmp.wav")
            synthesise(say, tmp, piper, voice)
            tmp.replace(cached)
        start = len(pcm) / 2 / RATE
        pcm += _frames(cached)
        timed.append(Timed(sentence, round(start, 3), round(len(pcm) / 2 / RATE, 3)))
        pcm += _silence(GAP)
    pcm = pcm[: len(pcm) - len(_silence(GAP))] + _silence(TAIL)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / f"scene-{scene.number:02d}.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(pcm))
    return SceneAudio(scene.number, wav, round(len(pcm) / 2 / RATE, 3), tuple(timed))


def join(scenes: Sequence[SceneAudio], out: Path) -> None:
    """All scenes, in order, as one WAV (the video's sound track)."""
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        for s in scenes:
            w.writeframes(_frames(s.wav))
