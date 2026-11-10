"""Scoring quality.tmf_completeness per check, from the ledger, against per-record ground
truth (the same method as meridian.evals.safety).

A finding is a true positive when its (check, record key) is a planted record of its cut,
read from the dedup key's check part "<check>:<record>". Clean cuts and DC-BASE have no
planted records, so every finding there is false. Harmless variation comes from the eTMF
snapshot's manifest (seed_record.harmless, document ids); a finding that names such a
document and is not a planted record counts as a harmless variation flagged.
"""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from meridian.data.cut_writer import CutManifest
from meridian.domain import tmf as t
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType
from meridian.domain.events import Event, FindingRaised
from meridian.faults.inject import GroundTruth
from meridian.faults.tmf import expected
from meridian.ledger.store import LedgerReader


@dataclass(slots=True)
class CheckScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return None if self.tp + self.fp == 0 else self.tp / (self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return None if self.tp + self.fn == 0 else self.tp / (self.tp + self.fn)


@dataclass(slots=True)
class TmfEval:
    name: str
    cuts: list[str]
    runs_missing: list[str] = field(default_factory=list)
    checks: dict[str, CheckScore] = field(
        default_factory=lambda: {c: CheckScore() for c in t.CHECKS}
    )
    findings: int = 0
    harmless_present: Counter[str] = field(default_factory=Counter)
    harmless_flagged: Counter[str] = field(default_factory=Counter)
    false: list[tuple[str, str, str]] = field(default_factory=list)  # cut, check, record
    missed: list[tuple[str, str, str]] = field(default_factory=list)

    def to_json(self) -> str:
        data = {
            "name": self.name,
            "cuts": self.cuts,
            "runs_missing": self.runs_missing,
            "findings": self.findings,
            "checks": {
                c: {
                    "tp": v.tp,
                    "fp": v.fp,
                    "fn": v.fn,
                    "precision": v.precision,
                    "recall": v.recall,
                }
                for c, v in self.checks.items()
            },
            "harmless_present": dict(sorted(self.harmless_present.items())),
            "harmless_flagged": dict(sorted(self.harmless_flagged.items())),
            "false": self.false,
            "missed": self.missed,
        }
        return json.dumps(data, indent=2, sort_keys=True) + "\n"


def evaluate(
    *,
    name: str,
    cuts: Sequence[str],
    ledger: LedgerReader,
    ground_truth_dir: Path,
    etmf_study_root: Path,
    agent_id: str,
    agent_version: str,
) -> TmfEval:
    result = TmfEval(name=name, cuts=list(cuts))
    wanted = set(cuts)
    by_cut: dict[str, list[tuple[str, str, FindingRaised]]] = {c: [] for c in cuts}
    ran: set[int] = set()
    seq_to_cut: dict[int, str] = {}
    for entry in ledger.read():
        if not isinstance(entry, Event):
            continue
        p = entry.payload
        if entry.event_type is EventType.DATA_CUT and getattr(p, "cut_id", None) in wanted:
            seq_to_cut[entry.seq] = p.cut_id  # type: ignore[union-attr]
        if (
            entry.event_type is EventType.ACTOR_SUSPENDED
            and getattr(p, "agent_id", None) == agent_id
            and getattr(p, "agent_version", None) == agent_version
        ):
            ran.add(p.trigger_seq)  # type: ignore[union-attr]
        if isinstance(p, FindingRaised):
            parts = parse_dedup_key(p.dedup_key)
            if (parts.agent_id, parts.agent_version) != (agent_id, agent_version):
                continue
            if parts.cut_id in wanted:
                check, _, record = parts.check.partition(":")
                by_cut[parts.cut_id].append((check, record, p))
    done = {cut for seq, cut in seq_to_cut.items() if seq in ran}
    result.runs_missing = [c for c in cuts if c not in done]
    for cut in cuts:
        truth_path = ground_truth_dir / f"{cut}.json"
        truth = expected(GroundTruth.load(truth_path)) if truth_path.exists() else set()
        got = {(check, record) for check, record, _ in by_cut[cut]}
        result.findings += len(by_cut[cut])
        for check, record in sorted(got | truth):
            score = result.checks.setdefault(check, CheckScore())
            if (check, record) in got and (check, record) in truth:
                score.tp += 1
            elif (check, record) in got:
                score.fp += 1
                result.false.append((cut, check, record))
            else:
                score.fn += 1
                result.missed.append((cut, check, record))
        manifest_dir = etmf_study_root / cut
        if not (manifest_dir / "manifest.json").exists():
            continue
        harmless: dict[str, list[str]] = CutManifest.load(manifest_dir).seed_record.get(
            "harmless", {}
        )
        for kind, ids in harmless.items():
            result.harmless_present[kind] += len(ids)
            for doc in ids:
                for check, record, raised in by_cut[cut]:
                    if (check, record) in truth:
                        continue
                    named = record.split("#")[0] == doc or any(
                        f"DOCUMENT_ID={doc}" in e.selector for e in raised.finding.evidence
                    )
                    if named:
                        result.harmless_flagged[kind] += 1
    return result


def table(results: Sequence[TmfEval]) -> str:
    """Markdown: precision and recall per check, one column pair per set; false findings
    for sets without planted faults."""

    def fmt(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.2f}"

    head = "| Check | " + " | ".join(
        f"{r.name} P | {r.name} R | {r.name} TP/FP/FN" for r in results
    )
    lines = [head + " |", "|---|" + "---|---|---|" * len(results)]
    for check in t.CHECKS:
        cells = []
        for r in results:
            c = r.checks.get(check, CheckScore())
            cells += [fmt(c.precision), fmt(c.recall), f"{c.tp}/{c.fp}/{c.fn}"]
        lines.append(f"| {check} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
