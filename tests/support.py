"""Shared builders for tests."""

from datetime import UTC, datetime
from pathlib import Path

from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.events import DataCut, EventDraft, FindingRaised
from meridian.domain.findings import Evidence, Finding
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import Clock
from meridian.platform.config import DEFAULT_FINDING_ID_PREFIXES

T0 = datetime(2026, 11, 7, 9, 0, 0, tzinfo=UTC)
STUDY = "MER-101"
BLINDED_ROLES = tuple(r for r in Role if r is not Role.UNBLINDED_STATISTICIAN)


def make_ledger(path: Path, clock: Clock) -> SqliteLedger:
    ledger = SqliteLedger(path=path, clock=clock, finding_id_prefixes=DEFAULT_FINDING_ID_PREFIXES)
    ledger.initialize()
    return ledger


def make_evidence(value: float = 0.2, threshold: float | None = 0.1) -> Evidence:
    return Evidence(
        source="data_cut:1",
        selector="site=SITE-001",
        statistic="query_rate",
        value=value,
        threshold=threshold,
    )


def make_finding(
    agent_id: str = "rbqm.kri_sentinel",
    visibility: Visibility = Visibility.BLINDED,
    visible_to: frozenset[Role] = frozenset({Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR}),
) -> Finding:
    return Finding(
        kind=FindingKind.KRI_BREACH,
        agent_id=agent_id,
        agent_version="1.0.0",
        study_id=STUDY,
        site_id="SITE-001",
        severity=Severity.MEDIUM,
        title="Query rate above KRI threshold",
        summary="Open query rate at SITE-001 is twice the study median.",
        evidence=(make_evidence(),),
        visibility=visibility,
        visible_to=visible_to,
        score=0.75,
    )


def data_cut_draft(n: int, visibility: Visibility = Visibility.BLINDED) -> EventDraft:
    return EventDraft(
        occurred_at=T0,
        actor="ingest.loader",
        study_id=STUDY,
        visibility=visibility,
        payload=DataCut(cut_id=f"CUT-{n}", study_id=STUDY, path=f"cuts/{n}.parquet"),
    )


def finding_draft(
    dedup_key: str,
    agent_id: str = "rbqm.kri_sentinel",
    visibility: Visibility = Visibility.BLINDED,
) -> EventDraft:
    visible_to = (
        frozenset({Role.UNBLINDED_STATISTICIAN})
        if visibility is Visibility.RESTRICTED
        else frozenset({Role.CLINICAL_OPS_LEAD})
    )
    return EventDraft(
        occurred_at=T0,
        actor=agent_id,
        study_id=STUDY,
        visibility=visibility,
        payload=FindingRaised(
            finding=make_finding(agent_id, visibility, visible_to), dedup_key=dedup_key
        ),
    )
