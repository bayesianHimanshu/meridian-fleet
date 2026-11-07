"""What a role may see. The only module in the API that decides it.

* A finding is visible when its visible_to contains the role and, if it is restricted,
  only to the unblinded statistician.
* A ledger event is shown as the ledger shows it: restricted events are redacted for
  every role but the unblinded statistician (SqliteLedger.read with viewer_role).
* A narrative or a decision is visible exactly when its finding is.
* Readable labels of opaque cut ids go only to roles that review the evaluation.

Repository functions call these; no route handler filters anything itself.
"""

from collections.abc import Iterable

from meridian.domain.enums import Role, Visibility
from meridian.domain.events import Event, LedgerEntry, RedactedEvent
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


def visible_findings(role: Role, findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if can_see_finding(role, f)]


def can_see_cut_label(role: Role) -> bool:
    return role in CUT_LABEL_ROLES


# Per-site RBQM metrics are blinded aggregates; the roles RBQM findings are shown to.
SITE_METRIC_ROLES = frozenset({Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.QA_AUDITOR})


def can_see_site_metrics(role: Role) -> bool:
    return role in SITE_METRIC_ROLES
