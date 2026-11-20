"""Recall, precision, false positives and average precision."""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from meridian.domain.enums import FindingKind
from meridian.evals.matching import FaultMatch, FindingRecord, MatchResult
from meridian.faults.catalog import FaultRecord

# Which agent is responsible for catching each kind of fault. Plain strings: evals does
# not import agents. Used to attribute missed faults when reporting per agent.
RESPONSIBLE_AGENT: dict[FindingKind, str] = {
    FindingKind.SITE_DATA_ANOMALY: "rbqm.csm",
    FindingKind.UNDER_REPORTING: "rbqm.kri_monitor",
    FindingKind.KRI_BREACH: "rbqm.kri_monitor",
    FindingKind.QTL_BREACH: "rbqm.qtl_detector",
}


class Metrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    faults: int
    findings: int
    true_positives: int
    false_positives: int
    missed: int
    recall: float | None
    precision: float | None
    average_precision: float | None


def average_precision(
    findings: Sequence[FindingRecord], true_ids: set[str], n_faults: int
) -> float | None:
    """Mean precision at each true positive, findings ranked by score (ties by seq).

    Missed faults contribute zero, so AP is also bounded by recall.
    """
    if n_faults == 0:
        return None
    ranked = sorted(findings, key=lambda f: (-f.score, f.seq))
    hits = 0
    total = 0.0
    for k, finding in enumerate(ranked, start=1):
        if finding.finding_id in true_ids:
            hits += 1
            total += hits / k
    return total / n_faults


def metrics(
    findings: Sequence[FindingRecord], matches: Sequence[FaultMatch], true_ids: set[str]
) -> Metrics:
    tp = sum(1 for f in findings if f.finding_id in true_ids)
    fp = len(findings) - tp
    n_faults = len(matches)
    detected = sum(1 for m in matches if m.detected)
    return Metrics(
        faults=n_faults,
        findings=len(findings),
        true_positives=tp,
        false_positives=fp,
        missed=n_faults - detected,
        recall=detected / n_faults if n_faults else None,
        precision=tp / len(findings) if findings else None,
        average_precision=average_precision(findings, true_ids, n_faults),
    )


class CutMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overall: Metrics
    per_agent: dict[str, Metrics]
    per_fault_type: dict[str, Metrics]


def cut_metrics(
    faults: Sequence[FaultRecord],
    findings: Sequence[FindingRecord],
    result: MatchResult,
    agents: Sequence[str],
) -> CutMetrics:
    true_ids = set(result.true_positives)
    per_agent: dict[str, Metrics] = {}
    for agent_id in sorted(set(agents) | {f.agent_id for f in findings}):
        owned = [
            m
            for m in result.matches
            if (m.agent_id or RESPONSIBLE_AGENT.get(m.expected_kind)) == agent_id
        ]
        per_agent[agent_id] = metrics(
            [f for f in findings if f.agent_id == agent_id], owned, true_ids
        )
    per_type: dict[str, Metrics] = {}
    for fault_type in sorted({f.fault_type for f in faults}):
        typed = [m for m in result.matches if m.fault_type == fault_type]
        ids = {m.finding_id for m in typed if m.finding_id}
        per_type[fault_type] = metrics([f for f in findings if f.finding_id in ids], typed, ids)
    return CutMetrics(
        overall=metrics(findings, result.matches, true_ids),
        per_agent=per_agent,
        per_fault_type=per_type,
    )
