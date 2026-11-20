"""The demo script (docs/demo/script.md) as data: scenes, their narration split into
sentences, and the highlight cues, each tied to the sentence whose words it names.

Pure: text in, Scenes out. The recorder and the TTS read the JSON this writes; nothing
here knows about pages or audio.
"""

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCRIPT_FILE = Path("docs/demo/script.md")
_SCENE = re.compile(r"^## Scene (\d+): (.+?) \(about \d+ s\)\s*$", re.M)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
EM_DASH = chr(0x2014)  # written as a code point: the character never appears in source


class ScriptError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Cue:
    """A highlight: while the sentence that contains `when` is spoken."""

    when: str
    element: str
    chip: str
    sentence: int


@dataclass(frozen=True, slots=True)
class Scene:
    number: int
    title: str
    sentences: tuple[str, ...]
    cues: tuple[Cue, ...] = field(default=())

    @property
    def narration(self) -> str:
        return " ".join(self.sentences)

    def words(self) -> int:
        return len(re.findall(r"[A-Za-z0-9'-]+", self.narration))


def _cell(text: str) -> str:
    return text.strip().strip("`").strip('"').strip()


def _cues(block: str, sentences: tuple[str, ...], number: int) -> tuple[Cue, ...]:
    """The rows of the scene's Highlights table (When | Element | Chip)."""
    out: list[Cue] = []
    for line in block.splitlines():
        cells = [c for c in line.split("|")[1:-1]]
        if len(cells) != 3 or set(cells[0].strip()) <= {"-"} or cells[0].strip() == "When":
            continue
        when, element, chip = (_cell(c) for c in cells)
        hit = [i for i, s in enumerate(sentences) if when.lower() in s.lower()]
        if not hit:
            raise ScriptError(f"scene {number}: highlight {when!r} names no sentence")
        out.append(Cue(when=when, element=element, chip=chip, sentence=hit[0]))
    return tuple(out)


def parse(text: str) -> list[Scene]:
    if EM_DASH in text:
        raise ScriptError("the script contains an em dash")
    heads = list(_SCENE.finditer(text))
    if not heads:
        raise ScriptError("no scenes found")
    scenes = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else text.find("\n## ", head.end())
        body = text[head.end() : end if end != -1 else len(text)]
        narration = re.search(r"\*\*Narration:\*\*\n(.*?)\n\n", body, re.S)
        if narration is None:
            raise ScriptError(f"scene {head.group(1)}: no narration")
        spoken = " ".join(
            line.lstrip(">").strip() for line in narration.group(1).splitlines()
        ).strip()
        sentences = tuple(s.strip() for s in _SENTENCE.split(spoken) if s.strip())
        highlights = re.search(r"\*\*Highlights:\*\*(.*?)(?:\n\*\*Sources|\Z)", body, re.S)
        cues = _cues(highlights.group(1), sentences, int(head.group(1))) if highlights else ()
        scenes.append(
            Scene(number=int(head.group(1)), title=head.group(2), sentences=sentences, cues=cues)
        )
    numbers = [s.number for s in scenes]
    if numbers != list(range(1, len(scenes) + 1)):
        raise ScriptError(f"scenes out of order: {numbers}")
    return scenes


def load(path: Path = SCRIPT_FILE) -> list[Scene]:
    return parse(path.read_text(encoding="utf-8"))


def as_json(scenes: list[Scene]) -> list[dict[str, object]]:
    return [
        {**asdict(s), "sentences": list(s.sentences), "cues": [asdict(c) for c in s.cues]}
        for s in scenes
    ]


# Spoken forms for the TTS (docs/demo/script.md, "Pronunciation list"). Applied to the
# audio only; captions and the transcript keep the written text.
PRONUNCIATION: tuple[tuple[str, str], ...] = (
    (r"\bNOAEL\b", "Noel"),
    (r"\bLOAEL\b", "Lo-ell"),
    (r"\bRBQM\b", "R B Q M"),
    (r"\bgVisor\b", "gee visor"),
    (r"\bopenFDA\b", "open F D A"),
    (r"\bCochran-Armitage\b", "Cockran Armitage"),
    (r"\bJonckheere-Terpstra\b", "Yonk-heera Terpstra"),
    (r"\bDunnett's\b", "Dunnit's"),
    (r"\bIND\b", "I N D"),
    (r"\bSAE\b", "S A E"),
    (r"\bMRD-101\b", "M R D one oh one"),
    (r"\bMRD-201\b", "M R D two oh one"),
    (r"\bFDA's\b", "F D A's"),
    (r"\bFDA\b", "F D A"),
    (r"\bseq\b", "sequence"),
)


def spoken(sentence: str) -> str:
    out = sentence
    for pattern, say in PRONUNCIATION:
        out = re.sub(pattern, say, out)
    return out
