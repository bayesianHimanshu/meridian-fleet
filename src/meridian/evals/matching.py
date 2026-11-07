"""Match findings to planted faults.

A finding matches a fault when it is on the same cut, its kind is the fault's
expected_kind, its site is the fault's target (or STUDY), and, for kri_breach, the check
in its dedup key is the fault's kri. Each fault takes at most one finding, the highest
scoring; any other finding that would also match is a duplicate and counts as a false
positive.
"""

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from meridian.domain.dedup import STUDY_SCOPE, parse_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role
from meridian.domain.events import Event, FindingRaised
from meridian.faults.catalog import FaultRecord
from meridian.ledger.store import LedgerReader

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class VersionFilter(BaseModel):
    """Which agent versions count: one fixed version for every agent, or a map per agent."""

    model_config = _FROZEN

    per_agent: dict[str, str] | None = None
    fixed: str | None = None

    def accepts(self, agent_id: str, agent_version: str) -> bool:
        if self.fixed is not None:
            return agent_version == self.fixed
        return self.per_agent is not None and self.per_agent.get(agent_id) == agent_version

    def label(self) -> str:
        if self.fixed is not None:
            return f"all agents at {self.fixed}"
        return ", ".join(f"{a}@{v}" for a, v in sorted((self.per_agent or {}).items()))

    @classmethod
    def registered(cls, versions: Mapping[str, str]) -> VersionFilter:
        return cls(per_agent=dict(versions))

    @classmethod
    def only(cls, version: str) -> VersionFilter:
        return cls(fixed=version)


class FindingRecord(BaseModel):
    model_config = _FROZEN

    seq: int
    finding_id: str
    agent_id: str
    agent_version: str
    kind: FindingKind
    site: str
    cut_id: str
    check: str
    severity: str
    score: float
    title: str


class FaultMatch(BaseModel):
    model_config = _FROZEN

    fault_id: str
    fault_type: str
    target: str
    expected_kind: FindingKind
    kri: str | None
    finding_id: str | None
    agent_id: str | None
    score: float | None
    duplicates: tuple[str, ...]

    @property
    def detected(self) -> bool:
        return self.finding_id is not None


class MatchResult(BaseModel):
    model_config = _FROZEN

    matches: tuple[FaultMatch, ...]
    true_positives: tuple[str, ...]  # finding ids
    false_positives: tuple[str, ...]  # finding ids, duplicates included


def findings_for_cut(
    reader: LedgerReader, cut_id: str, versions: VersionFilter
) -> list[FindingRecord]:
    """Every finding raised on cut_id by an accepted agent version, in ledger order."""
    out: list[FindingRecord] = []
    for entry in reader.read(
        event_types=[EventType.FINDING_RAISED], viewer_role=Role.UNBLINDED_STATISTICIAN
    ):
        if not isinstance(entry, Event) or not isinstance(entry.payload, FindingRaised):
            continue
        parts = parse_dedup_key(entry.payload.dedup_key)
        finding = entry.payload.finding
        if parts.cut_id != cut_id:
            continue
        if not versions.accepts(finding.agent_id, finding.agent_version):
            continue
        out.append(
            FindingRecord(
                seq=entry.seq,
                finding_id=finding.finding_id or f"seq-{entry.seq}",
                agent_id=finding.agent_id,
                agent_version=finding.agent_version,
                kind=finding.kind,
                site=finding.site_id or STUDY_SCOPE,
                cut_id=parts.cut_id,
                check=parts.check,
                severity=finding.severity.value,
                score=finding.score,
                title=finding.title,
            )
        )
    return out


def _matches(fault: FaultRecord, finding: FindingRecord) -> bool:
    if finding.kind is not fault.expected_kind or finding.site != fault.target:
        return False
    return not (fault.expected_kind is FindingKind.KRI_BREACH and finding.check != fault.kri)


def match_faults(faults: Sequence[FaultRecord], findings: Sequence[FindingRecord]) -> MatchResult:
    used: set[str] = set()
    matches: list[FaultMatch] = []
    duplicates: set[str] = set()
    for fault in faults:
        candidates = sorted(
            (f for f in findings if f.finding_id not in used and _matches(fault, f)),
            key=lambda f: (-f.score, f.seq),
        )
        best = candidates[0] if candidates else None
        if best is not None:
            used.add(best.finding_id)
        extra = tuple(f.finding_id for f in candidates[1:])
        duplicates.update(extra)
        used.update(extra)
        matches.append(
            FaultMatch(
                fault_id=fault.fault_id,
                fault_type=fault.fault_type,
                target=fault.target,
                expected_kind=fault.expected_kind,
                kri=fault.kri,
                finding_id=best.finding_id if best else None,
                agent_id=best.agent_id if best else None,
                score=best.score if best else None,
                duplicates=extra,
            )
        )
    tp = tuple(m.finding_id for m in matches if m.finding_id is not None)
    fp = tuple(f.finding_id for f in findings if f.finding_id not in tp)
    return MatchResult(matches=tuple(matches), true_positives=tp, false_positives=fp)
