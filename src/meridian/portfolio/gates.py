"""Stage-gate criteria (config/gates.toml) and their rules (phase 5 step 3b;
docs/stage-gate-notes.md). Pure: facts in, each criterion met, not met or not applicable
with its seqs, and the open items out. No verdict: the portfolio lead decides."""

import os
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.domain.display import display_doses
from meridian.domain.events import GatePack
from meridian.portfolio.facts import CLOSED, OPEN, Fact

GATES_FILE = "gates.toml"
Status = Literal["met", "not_met", "not_applicable"]


class Criterion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z_]+$")
    title: str = Field(min_length=1)
    basis: Literal["regulation", "policy"]
    source: str | None = None
    section: str | None = None
    quote: str | None = None
    rule: str
    evidence: str = Field(min_length=1)
    applies: Literal["always", "has_signal_scan"] = "always"

    @model_validator(mode="after")
    def _cited(self) -> Criterion:
        if self.basis == "regulation" and not (self.source and self.section and self.quote):
            raise ValueError(f"{self.id}: a regulation criterion needs source, section, quote")
        if self.rule not in RULES:
            raise ValueError(f"{self.id}: unknown rule {self.rule}")
        return self


class Gate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z_]+$")
    asset_id: str
    title: str
    criteria: tuple[Criterion, ...] = Field(min_length=1)


class Gates(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gates: tuple[Gate, ...] = ()

    def get(self, asset_id: str, gate: str) -> Gate | None:
        return next((g for g in self.gates if g.asset_id == asset_id and g.id == gate), None)


def load_gates(path: Path | None = None) -> Gates:
    """config/gates.toml next to the config file in use."""
    config = os.environ.get("MERIDIAN_CONFIG_FILE")
    default = (Path(config).parent if config else Path("config")) / GATES_FILE
    with (path or default).open("rb") as handle:
        return Gates.model_validate(tomllib.load(handle))


@dataclass(frozen=True, slots=True)
class Item:
    text: str
    seq: int | None = None
    finding_id: str | None = None


@dataclass(frozen=True, slots=True)
class Result:
    criterion: Criterion
    status: Status
    explanation: str
    seqs: tuple[int, ...]
    open_items: tuple[Item, ...]


def _seqs(rows: Sequence[Fact]) -> tuple[int, ...]:
    return tuple(sorted({r.seq for r in rows if r.seq is not None}))


Rule = Callable[[list[Fact]], tuple[Status, str, list[Fact], list[Item]]]


def _group(facts: list[Fact], group: str) -> list[Fact]:
    return [f for f in facts if f.group == group]


def _noael(facts: list[Fact]) -> tuple[Status, str, list[Fact], list[Item]]:
    rows = _group(facts, "noael")
    if not rows:
        return "not_met", "No NOAEL decision is recorded.", [], [Item("Record the NOAEL decision")]
    told = "; ".join(f"{r.label}: {r.value}" for r in rows)
    return "met", f"Current NOAEL decision: {told}.", rows, []


def _decided(group: str, what: str) -> Rule:
    def rule(facts: list[Fact]) -> tuple[Status, str, list[Fact], list[Item]]:
        rows = _group(facts, group)
        if not rows:
            return "not_met", f"No {what} is recorded.", [], [Item(f"No {what} yet")]
        r = rows[-1]
        name = r.finding_id or what
        if r.status == "accepted":
            return "met", f"{name} ({r.value}) is accepted.", rows, []
        return (
            "not_met",
            f"{name} ({r.value}) is {r.status or 'not decided'}, not accepted.",
            rows,
            [Item(f"{name} is {r.status or 'not decided'}", r.seq, r.finding_id)],
        )

    return rule


def _gaps(facts: list[Fact]) -> tuple[Status, str, list[Fact], list[Item]]:
    summary = _group(facts, "ind_readiness")
    gaps = _group(facts, "ind_gap")
    if not summary:
        return "not_met", "No inventory is assessed.", gaps, [Item("Assess the inventory")]
    open_ = [g for g in gaps if g.status not in CLOSED]
    excepted = [g for g in gaps if g.status == "accepted"]
    dismissed = [g for g in gaps if g.status == "dismissed"]
    used = [*summary, *gaps]
    if open_:
        return (
            "not_met",
            f"{len(open_)} gap(s) open of {len(gaps)}.",
            used,
            [Item(f"Gap {g.finding_id or g.key} ({g.label}) is {g.status}", g.seq, g.finding_id)
             for g in open_],
        )  # fmt: skip
    text = (
        f"No gap open: {len(excepted)} accepted as an exception, {len(dismissed)} closed."
        if gaps
        else "No gap: every requirement is met by the inventory."
    )
    return "met", text, used, []


def _high(facts: list[Fact]) -> tuple[Status, str, list[Fact], list[Item]]:
    rows = [f for f in _group(facts, "open_finding") if f.severity == "high" and f.status in OPEN]
    if rows:
        return (
            "not_met",
            f"{len(rows)} high-severity preclinical finding(s) without a closing decision.",
            rows,
            [Item(f"{r.finding_id or r.key} ({r.label}) is {r.status}", r.seq, r.finding_id)
             for r in rows],
        )  # fmt: skip
    return "met", "No open high-severity preclinical finding.", [], []


def _signals(facts: list[Fact]) -> tuple[Status, str, list[Fact], list[Item]]:
    scan = _group(facts, "signal_scan")
    rows = _group(facts, "priority_signal")
    open_ = [r for r in rows if r.status == "open" or not r.status]
    if open_:
        return (
            "not_met",
            f"{len(open_)} of {len(rows)} priority signal(s) not yet reviewed.",
            [*scan, *rows],
            [Item(f"Signal {r.finding_id or r.key} ({r.label}) is open", r.seq, r.finding_id)
             for r in open_],
        )  # fmt: skip
    return "met", f"All {len(rows)} priority signal(s) reviewed.", [*scan, *rows], []


RULES: dict[str, Rule] = {
    "noael_decided": _noael,
    "starting_dose_accepted": _decided("starting_dose", "dose proposal"),
    "ind_gaps_resolved": _gaps,
    "monitoring_accepted": _decided("monitoring", "monitoring recommendation"),
    "no_open_high_preclinical": _high,
    "safety_signals_reviewed": _signals,
}


def _applies(c: Criterion, facts: list[Fact]) -> bool:
    if c.applies == "has_signal_scan":
        return bool(_group(facts, "signal_scan"))
    return True


def evaluate(gate: Gate, facts: Sequence[Fact]) -> list[Result]:
    mine = [f for f in facts if f.asset_id == gate.asset_id]
    out = []
    for c in gate.criteria:
        if not _applies(c, mine):
            out.append(
                Result(c, "not_applicable", "Not applicable: the asset has no safety signal "
                       "scan (no marketed drug with spontaneous reports).", (), ())
            )  # fmt: skip
            continue
        status, why, used, items = RULES[c.rule](mine)
        out.append(Result(c, status, why, _seqs(used), tuple(items)))
    return out


def pack_for_display(pack: GatePack) -> GatePack:
    """A recorded pack as a person reads it (wrap-up): doses at 3 significant figures in
    its texts. The ledger event keeps them as recorded."""
    return pack.model_copy(
        update={
            "summary": display_doses(pack.summary),
            "criteria": tuple(
                c.model_copy(update={"explanation": display_doses(c.explanation)})
                for c in pack.criteria
            ),
            "open_items": tuple(
                o.model_copy(update={"text": display_doses(o.text)}) for o in pack.open_items
            ),
        }
    )
