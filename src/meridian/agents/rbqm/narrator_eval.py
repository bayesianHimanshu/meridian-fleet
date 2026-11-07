"""The narrator validator evaluation: labelled narratives, seeded mutations, metrics.

Kept with the narrator, not in meridian.evals: it has to run the validators it measures,
and meridian.evals (detection scoring) may not import agent code.

A labelled record is one narrative with its label (good or bad), the reason codes a
correct validator should give, and where it came from: real_good and real_bad are
narrator 1.0.0 narratives labelled by hand (assistant-judged, docs/narrator-labels.md);
mutated records are real_good narratives with one deterministic error introduced, labelled
bad. Each record carries the input as each narrator version sees it, so each validator is
run on what its own narrator would have been given.
"""

import random
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from meridian.agents.rbqm import narrator as v1_1
from meridian.agents.rbqm import narrator_v1_0 as v1_0
from meridian.agents.rbqm.narrator_v1_0 import numbers_in

Origin = Literal["real_good", "real_bad", "mutated"]
MUTATIONS: tuple[str, ...] = (
    "wrong_number",
    "flip_direction",
    "swap_site",
    "unbacked_ratio",
    "supporting_headline",
    "zero_p",
)
EXPECTED: dict[str, tuple[str, ...]] = {
    "wrong_number": ("number_not_displayed",),
    "flip_direction": ("direction_contradicted",),
    "swap_site": ("missing_site",),
    "unbacked_ratio": ("unbacked_ratio",),
    "supporting_headline": ("supporting_as_headline",),
    "zero_p": ("zero_p_value",),
}
# A 1.0.0 code that means the same as a 1.1.0 one, for "rejected for the expected reason".
SAME_AS: dict[str, str] = {"number_not_in_evidence": "number_not_displayed"}


class Narrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    headline: str
    explanation: str


class LabelledRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    finding_id: str
    cut: str
    origin: Origin
    mutation: str | None = None
    label: Literal["good", "bad"]
    expected_reasons: tuple[str, ...]
    labelled_by: Literal["assistant", "generator"]
    note: str
    narrative: Narrative
    input_1_0: dict[str, Any]
    input_1_1: dict[str, Any]


# ---- mutations

_FLIP = {
    "fewer": "more",
    "more": "fewer",
    "less": "more",
    "smaller": "larger",
    "larger": "smaller",
    "lower": "higher",
    "higher": "lower",
    "below": "above",
    "above": "below",
    "little": "much",
}
_FLIP_WORD = re.compile(r"\b(" + "|".join(_FLIP) + r")\b", re.IGNORECASE)
_RATIO_PHRASES = (
    "twice the expected level",
    "three times the usual rate",
    "half the level at the other sites",
    "a 4-fold difference from the other sites",
)
_P_NUMBER = re.compile(
    r"\b(p(?:[- ]?values?)?\s*(?:=|of)\s*)(\d+(?:\.\d+)?(?:e[-+]?\d+)?|below 1e-12)",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[.!?](?=\s|$)")


def _rng(seed: int, record_id: str, mutation: str) -> random.Random:
    return random.Random(f"{seed}|{record_id}|{mutation}")  # noqa: S311 (not for security)


def _case(src: str, word: str) -> str:
    return word.capitalize() if src[:1].isupper() else word


def _nearby(text: str, rng: random.Random) -> str:
    """A different number of the same shape: one digit of the mantissa moved."""
    mantissa, e, exp = text.partition("e")
    digits = [i for i, c in enumerate(mantissa) if c.isdigit()]
    i = digits[-1]
    if not ("." in mantissa or e) and len(digits) > 1:
        i = rng.choice(digits[1:])  # keep the leading digit of an integer
    step = rng.choice((1, 2, 3))
    new = str((int(mantissa[i]) + step) % 10)
    if i == digits[0] and new == "0":
        new = str((int(mantissa[i]) + step + 1) % 10)
    return mantissa[:i] + new + mantissa[i + 1 :] + e + exp


def _numbers_of(inp: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    stack: list[object] = [inp]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            stack += list(x.values())
        elif isinstance(x, list):
            stack += x
        elif isinstance(x, str):
            out |= {t.text.lstrip("-") for t in numbers_in(x.replace("_", " "))}
        elif isinstance(x, float | int) and not isinstance(x, bool):
            out.add(f"{x:g}")
    return out


def mutate(
    record: LabelledRecord, mutation: str, seed: int, sites: Sequence[str]
) -> LabelledRecord | None:
    """One deterministic error introduced into a good narrative, or None if this mutation
    does not apply (for example no p value to replace)."""
    rng = _rng(seed, record.id, mutation)
    h, x = record.narrative.headline, record.narrative.explanation
    site = record.input_1_1.get("site_id")
    note = ""
    if mutation == "wrong_number":
        taken = _numbers_of(record.input_1_0) | _numbers_of(record.input_1_1)
        tokens = [
            m for m in re.finditer(r"(?<![\w.])\d+(?:\.\d+)?(?:e-\d+)?", x) if m.group(0) != site
        ]
        if not tokens:
            return None
        m = rng.choice(tokens)
        for _ in range(10):
            new = _nearby(m.group(0), rng)
            if new != m.group(0) and new not in taken:
                break
        else:
            return None
        x = x[: m.start()] + new + x[m.end() :]
        note = f"{m.group(0)} -> {new}"
    elif mutation == "flip_direction":
        if not _FLIP_WORD.search(h + x):
            return None

        def flip(m: re.Match[str]) -> str:
            return _case(m.group(0), _FLIP[m.group(0).lower()])

        h, x = _FLIP_WORD.sub(flip, h), _FLIP_WORD.sub(flip, x)
        note = "direction words flipped"
    elif mutation == "swap_site":
        if not site:
            return None
        other = rng.choice(sorted(set(sites) - {str(site)}))
        pattern = re.compile(rf"(?<![\w-]){re.escape(str(site))}(?![\w-])")
        h, x = pattern.sub(other, h), pattern.sub(other, x)
        note = f"site {site} -> {other}"
    elif mutation == "unbacked_ratio":
        phrase = rng.choice(_RATIO_PHRASES)
        end = _SENTENCE.search(x)
        cut = end.start() if end else len(x)
        x = x[:cut] + f", {phrase}" + x[cut:]
        note = f"inserted '{phrase}'"
    elif mutation == "supporting_headline":
        supporting = [
            str(e["display"])
            for e in record.input_1_1.get("evidence", [])
            if e.get("role") == "supporting" and e.get("display")
        ]
        prefix = f"Site {site}: " if site else "Study-wide: "
        options = [d for d in supporting if len(prefix + d) <= v1_0.MAX_HEADLINE]
        if not options:
            return None
        display = rng.choice(options)
        h = prefix + display
        note = f"headline -> '{h}'"
    elif mutation == "zero_p":
        found = list(_P_NUMBER.finditer(h + "\n" + x))
        if not found:
            return None
        m = rng.choice(found)
        both = h + "\n" + x
        both = both[: m.start(2)] + "0" + both[m.end(2) :]
        h, x = both.split("\n", 1)
        note = f"p {m.group(2)} -> 0"
    else:
        raise ValueError(f"unknown mutation {mutation!r}")
    return record.model_copy(
        update={
            "id": f"{record.finding_id}/mutated/{mutation}",
            "origin": "mutated",
            "mutation": mutation,
            "label": "bad",
            "expected_reasons": EXPECTED[mutation],
            "labelled_by": "generator",
            "note": note,
            "narrative": Narrative(headline=h, explanation=x),
        }
    )


def mutations_of(
    good: Sequence[LabelledRecord], seed: int, sites: Sequence[str]
) -> list[LabelledRecord]:
    out = []
    for record in good:
        for mutation in MUTATIONS:
            m = mutate(record, mutation, seed, sites)
            if m is not None:
                out.append(m)
    return out


# ---- running validators and metrics

Validator = Callable[[LabelledRecord], tuple[str, ...]]


def _codes(reasons: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(SAME_AS.get(c, c) for c in v1_1.reason_codes(reasons)))


def _reply(record: LabelledRecord) -> str:
    return record.narrative.model_dump_json()


def validator_1_0(record: LabelledRecord) -> tuple[str, ...]:
    return _codes(v1_0.validate(_reply(record), record.input_1_0).reasons)


def validator_1_1(record: LabelledRecord) -> tuple[str, ...]:
    return _codes(v1_1.validate(_reply(record), record.input_1_1).reasons)


VALIDATORS: dict[str, Validator] = {"1.0.0": validator_1_0, "1.1.0": validator_1_1}


@dataclass(frozen=True, slots=True)
class Verdict:
    record: LabelledRecord
    codes: tuple[str, ...]

    @property
    def rejected(self) -> bool:
        return bool(self.codes)

    @property
    def for_expected_reason(self) -> bool:
        return bool(set(self.record.expected_reasons) & set(self.codes))


@dataclass(frozen=True, slots=True)
class Rate:
    hits: int
    total: int

    @property
    def value(self) -> float | None:
        return self.hits / self.total if self.total else None

    def __str__(self) -> str:
        v = self.value
        return "n/a (0)" if v is None else f"{v:.2f} ({self.hits}/{self.total})"


@dataclass(slots=True)
class Metrics:
    verdicts: list[Verdict]
    recall: Rate = field(default_factory=lambda: Rate(0, 0))
    recall_by_origin: dict[str, Rate] = field(default_factory=dict)
    recall_by_mutation: dict[str, Rate] = field(default_factory=dict)
    # Mutations of narratives the validator accepted unmutated: a rejection there can only
    # come from the mutation.
    recall_by_mutation_clean_base: dict[str, Rate] = field(default_factory=dict)
    false_rejections: list[Verdict] = field(default_factory=list)
    good_total: int = 0
    expected_reason: Counter[str] = field(default_factory=Counter)
    confusion: Counter[tuple[str, str]] = field(default_factory=Counter)


def evaluate(records: Sequence[LabelledRecord], validator: Validator) -> Metrics:
    verdicts = [Verdict(r, validator(r)) for r in records]
    bad = [v for v in verdicts if v.record.label == "bad"]
    good = [v for v in verdicts if v.record.label == "good"]
    base_ok = {v.record.finding_id for v in good if not v.rejected}
    m = Metrics(verdicts=verdicts)
    m.recall = Rate(sum(v.rejected for v in bad), len(bad))
    for origin in ("real_bad", "mutated"):
        sub = [v for v in bad if v.record.origin == origin]
        m.recall_by_origin[origin] = Rate(sum(v.rejected for v in sub), len(sub))
    for mutation in MUTATIONS:
        sub = [v for v in bad if v.record.mutation == mutation]
        m.recall_by_mutation[mutation] = Rate(sum(v.rejected for v in sub), len(sub))
        clean = [v for v in sub if v.record.finding_id in base_ok]
        m.recall_by_mutation_clean_base[mutation] = Rate(sum(v.rejected for v in clean), len(clean))
    m.good_total = len(good)
    m.false_rejections = [v for v in good if v.rejected]
    for v in bad:
        if not v.rejected:
            outcome = "accepted"
        elif not v.record.expected_reasons:
            outcome = "rejected (no expected code)"
        elif v.for_expected_reason:
            outcome = "rejected for the expected reason"
        else:
            outcome = "rejected for another reason only"
        m.expected_reason[outcome] += 1
        for expected in v.record.expected_reasons or ("(none)",):
            for got in v.codes or ("accepted",):
                m.confusion[(expected, got)] += 1
    return m
