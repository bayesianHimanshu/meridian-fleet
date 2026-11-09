"""What a role may see. The only module in the API that decides it.

* A finding is visible when its visible_to contains the role and, if it is restricted,
  only to the unblinded statistician.
* A ledger event is shown as the ledger shows it: restricted events are redacted for
  every role but the unblinded statistician (SqliteLedger.read with viewer_role).
* A narrative or a decision is visible exactly when its finding is; an assessment
  (FindingAssessment, about one or two findings) exactly when all of its findings are.
* In the ledger, an event about a finding the role may not see (its FindingRaised, a
  narrative or a decision) is redacted like a restricted event: a subject-level safety
  finding carries a subject id, and the ledger must not show it to portfolio_lead.
* Readable labels of opaque cut ids go only to roles that review the evaluation.

Repository functions call these; no route handler filters anything itself.
"""

from collections.abc import Collection, Iterable

from meridian.domain.enums import Role, Visibility
from meridian.domain.events import (
    Event,
    FindingAssessment,
    FindingNarrative,
    FindingRaised,
    HumanDecision,
    LedgerEntry,
    RedactedEvent,
)
from meridian.domain.findings import Finding

FULL_ACCESS = Role.UNBLINDED_STATISTICIAN
# Who may read the readable label of an opaque cut id (it names the evaluation design,
# for example "rbqm_holdout2"; fault labels and ground truth are never served at all).
CUT_LABEL_ROLES = frozenset({Role.QA_AUDITOR})


def ledger_viewer(role: Role) -> Role:
    """The viewer_role to read the ledger with: redaction is the ledger's own."""
    return role


def can_see_finding(role: Role, finding: Finding) -> bool:
    if role not in finding.visible_to:
        return False
    return finding.visibility is Visibility.BLINDED or role is FULL_ACCESS


def can_see_event(role: Role, entry: LedgerEntry) -> bool:
    """Whether the entry may be shown unredacted. A RedactedEvent is always shown as such."""
    if isinstance(entry, RedactedEvent):
        return False
    return entry.visibility is Visibility.BLINDED or role is FULL_ACCESS


def redact(role: Role, entry: LedgerEntry) -> LedgerEntry:
    """The entry as role may see it: a restricted event becomes a RedactedEvent."""
    if isinstance(entry, RedactedEvent) or can_see_event(role, entry):
        return entry
    assert isinstance(entry, Event)
    return RedactedEvent(
        seq=entry.seq,
        event_type=entry.event_type,
        recorded_at=entry.recorded_at,
        hash=entry.hash,
        prev_hash=entry.prev_hash,
    )


def finding_id_of(entry: Event) -> str | None:
    """The finding an event is about, if any (the first, for an assessment)."""
    ids = finding_ids_of(entry)
    return ids[0] if ids else None


def finding_ids_of(entry: Event) -> tuple[str, ...]:
    """Every finding an event is about."""
    p = entry.payload
    if isinstance(p, FindingRaised):
        return () if p.finding.finding_id is None else (p.finding.finding_id,)
    if isinstance(p, FindingNarrative):
        return (p.finding_id,)
    if isinstance(p, FindingAssessment):
        return p.finding_ids
    if isinstance(p, HumanDecision):
        return (p.decision.finding_id,)
    return ()


def can_see_assessment(assessment: FindingAssessment, visible_finding_ids: Collection[str]) -> bool:
    return all(f in visible_finding_ids for f in assessment.finding_ids)


def redact_with_findings(
    role: Role, entry: LedgerEntry, visible_finding_ids: Collection[str]
) -> LedgerEntry:
    """redact, and also redact any event about a finding outside visible_finding_ids
    (the findings this role may see)."""
    shown = redact(role, entry)
    if isinstance(shown, Event) and any(
        f not in visible_finding_ids for f in finding_ids_of(shown)
    ):
        return RedactedEvent(
            seq=shown.seq,
            event_type=shown.event_type,
            recorded_at=shown.recorded_at,
            hash=shown.hash,
            prev_hash=shown.prev_hash,
        )
    return shown


# The count of safety cases that carry an unblinded treatment field (never the field).
def can_see_unblinded_counts(role: Role) -> bool:
    return role is FULL_ACCESS


def visible_findings(role: Role, findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if can_see_finding(role, f)]


def can_see_cut_label(role: Role) -> bool:
    return role in CUT_LABEL_ROLES


# Per-site RBQM metrics and study enrollment are blinded aggregates of the study's data;
# the roles RBQM findings are shown to.
SITE_METRIC_ROLES = frozenset({Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.QA_AUDITOR})


def can_see_site_metrics(role: Role) -> bool:
    return role in SITE_METRIC_ROLES


def can_see_study_data(role: Role) -> bool:
    return role in SITE_METRIC_ROLES
