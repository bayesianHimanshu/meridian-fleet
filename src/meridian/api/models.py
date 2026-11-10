"""Response and request models of the Fleet API (the OpenAPI contract with the UI)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.domain.enums import DecisionAction, EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import (
    AssessmentCandidate,
    AssessmentStatus,
    Event,
    NarrativeStatus,
    RedactedEvent,
    Verdict,
)
from meridian.domain.findings import Evidence

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# retired: a newer version of the agent ran on the cut and raised nothing with the same
# (kind, site, check), and no person closed the finding.
FindingStatus = Literal["open", "accepted", "escalated", "dismissed", "retired"]
CutKind = Literal["base", "null", "faulted", "holdout", "sweep"]
# operational: the study's current data. evaluation: null, faulted, holdout and sweep cuts.
CutScope = Literal["operational", "evaluation"]


class Health(BaseModel):
    model_config = _FROZEN

    status: Literal["ok"] = "ok"
    auth: Literal["development"] = "development"


class WhoAmI(BaseModel):
    model_config = _FROZEN

    actor: str
    name: str
    role: Role
    auth: Literal["development"] = "development"


class PersonView(BaseModel):
    """A demo person from config/people.toml (development identity, not an account)."""

    model_config = _FROZEN

    actor: str
    name: str
    role: Role


class StudySummary(BaseModel):
    model_config = _FROZEN

    study_id: str
    cuts: int


class CutSummary(BaseModel):
    model_config = _FROZEN

    cut_id: str
    trigger_seq: int
    # None for an opaque cut id when the caller may not see its label.
    kind: CutKind | None
    label: str | None
    opaque: bool
    scope: CutScope
    created: datetime
    agent_runs_completed: int


class LatestNarrative(BaseModel):
    model_config = _FROZEN

    narrator_version: str
    headline: str
    explanation: str
    seq: int
    # Who wrote it (rbqm.narrator, the cloud model; safety.local_narrator, the local
    # model) and with which model.
    narrator_id: str = ""
    model: str = ""


class FindingSummary(BaseModel):
    model_config = _FROZEN

    finding_id: str
    seq: int
    raised_at: datetime
    study_id: str
    cut_id: str
    scope: CutScope
    check: str
    agent_id: str
    agent_version: str
    # A newer version of the same agent raised a finding with the same (cut, kind, site,
    # check); superseded_by is the newest one's id.
    superseded: bool
    superseded_by: str | None
    retired: bool
    retired_by_version: str | None  # the newer agent version whose run raised nothing
    kind: FindingKind
    site_id: str | None
    severity: Severity
    title: str
    summary: str
    score: float
    visibility: Visibility
    visible_to: list[Role]
    status: FindingStatus
    decision_count: int
    latest_narrative: LatestNarrative | None
    # Display strings of the primary evidence (described the same way for old findings).
    primary_evidence: list[str]


class FindingPage(BaseModel):
    model_config = _FROZEN

    items: list[FindingSummary]
    total: int
    limit: int
    offset: int


class NarrativeView(BaseModel):
    model_config = _FROZEN

    seq: int
    recorded_at: datetime
    narrator_version: str
    status: NarrativeStatus
    headline: str | None
    explanation: str | None
    model: str
    rejection_reasons: list[str]
    executor: str | None
    sandbox: str | None
    narrator_id: str = ""
    # The digest the local model was served with; null for the cloud narrator.
    model_digest: str | None = None


class AnswerView(BaseModel):
    """One model's answer inside an agreement assessment (term adjudicator 1.1.0): its raw
    text as received, and its verdict and reason only if it passed validation."""

    model_config = _FROZEN

    model: str
    model_digest: str | None
    status: AssessmentStatus
    verdict: Verdict | None
    reason: str | None
    text: str | None
    rejection_reasons: list[str]
    error: str | None
    latency_ms: int | None


class AssessmentView(BaseModel):
    """A local model's judgement about this finding (and, for a pair, one other). Shown
    only when the role may see every finding it names. For 1.0.0 (one model) a rejected or
    failed answer shows its status and reason codes, never the model's text; for 1.1.0
    (agreement) answers holds each model's raw answer, and verdict and reason are the
    agreement rule's."""

    model_config = _FROZEN

    seq: int
    recorded_at: datetime
    agent_id: str
    agent_version: str
    candidate: AssessmentCandidate
    finding_ids: list[str]
    status: AssessmentStatus
    verdict: Verdict | None
    reason: str | None
    model: str
    model_digest: str | None
    rejection_reasons: list[str]
    error: str | None
    executor: str | None
    sandbox: str | None
    answers: list[AnswerView] = []


class DecisionView(BaseModel):
    model_config = _FROZEN

    seq: int
    hash: str
    prev_hash: str
    recorded_at: datetime
    actor: str
    role: Role
    action: DecisionAction
    reason: str | None


class RunEvent(BaseModel):
    model_config = _FROZEN

    seq: int
    hash: str
    recorded_at: datetime
    executor: str | None
    image_digest: str | None = None
    # sha256 of the policy the sandbox reported as effective (comments dropped by
    # OpenShell), and the committed file passed to `openshell sandbox create` with the
    # sha256 of its bytes. The file pair is null on runs recorded before step 3a polish.
    applied_policy_sha256: str | None = None
    policy_file: str | None = None
    policy_file_sha256: str | None = None
    inputs_manifest_sha256: str | None = None


class RunTrace(BaseModel):
    """The run that raised the finding: its ActorWoke and ActorSuspended events."""

    model_config = _FROZEN

    agent_id: str
    agent_version: str
    trigger_seq: int
    cut_id: str
    woke: RunEvent | None
    suspended: RunEvent | None


class FindingDetail(BaseModel):
    model_config = _FROZEN

    finding: FindingSummary
    dedup_key: str
    hash: str
    evidence: list[Evidence]
    narratives: list[NarrativeView]
    assessments: list[AssessmentView] = []
    decisions: list[DecisionView]
    run: RunTrace | None


class DecisionRequest(BaseModel):
    model_config = _FROZEN

    action: DecisionAction
    reason: Annotated[str, Field(max_length=2000)] | None = None


class DecisionResponse(BaseModel):
    model_config = _FROZEN

    finding_id: str
    status: FindingStatus
    decision: DecisionView


class ReviewQueue(BaseModel):
    model_config = _FROZEN

    items: list[FindingSummary]


class LedgerPage(BaseModel):
    model_config = _FROZEN

    entries: list[Event | RedactedEvent]
    # order=asc: pass as ?since= to continue; None when this page reached the end.
    next_since: int | None
    # order=desc: pass as ?before= for the next (older) page; None at the first event.
    next_before: int | None = None


class VerifyReport(BaseModel):
    model_config = _FROZEN

    ok: bool
    events_checked: int
    first_broken_seq: int | None
    problem: str | None


class FleetAgent(BaseModel):
    model_config = _FROZEN

    agent_id: str
    version: str
    zone: str
    role: Literal["detector", "narrator", "adjudicator"]
    enabled: bool
    policy: str  # the committed policy's name, e.g. rbqm or safety.sae_reconciler
    policy_file: str
    # sha256 of the committed file's bytes (comments included), and of what OpenShell
    # reports as effective for it (comments dropped): the value a run records.
    policy_file_sha256: str
    applied_policy_sha256: str
    last_run_at: datetime | None
    last_outcome: (
        Literal["completed", "not_completed", "accepted", "rejected", "refused", "error"] | None
    )
    last_trigger_seq: int | None
    # Every version of the agent. A detector run failed when its ActorWoke has no
    # ActorSuspended; a narrator or adjudicator run failed when it recorded status
    # "error".
    runs_7d: int
    failed_7d: int
    last_failure_at: datetime | None
    # The ledger event types that record this agent's runs (for a filtered ledger link).
    run_event_types: list[EventType]


class ContainmentCheckView(BaseModel):
    model_config = _FROZEN

    name: str
    expect: Literal["refused", "allowed"]
    outcome: Literal["refused", "allowed", "inconclusive"]
    passed: bool
    message: str


class ContainmentView(BaseModel):
    model_config = _FROZEN

    policy: str
    zone: str
    seq: int
    recorded_at: datetime
    sandbox: str
    image_digest: str
    applied_policy_sha256: str | None  # what the conformance sandbox reported as effective
    passed: bool
    all_refused: bool
    log_line_found: bool
    credential_value_visible: bool | None
    checks: list[ContainmentCheckView]


class Fleet(BaseModel):
    model_config = _FROZEN

    agents: list[FleetAgent]
    containment: list[ContainmentView]


class SiteFlag(BaseModel):
    """A current finding (newest version, not superseded, not retired) at this site."""

    model_config = _FROZEN

    check: str
    finding_id: str
    agent_id: str
    agent_version: str
    status: str
    # For a vital-sign variance finding: the parameters it rests on (SYSBP, ...).
    parameters: list[str]


# untested_direction: a value on the side the agent does not test (no fill).
# too_few: below the minimum count the agent needs before it tests at all (no fill).
Band = Literal["normal", "warning", "critical", "untested_direction", "too_few", "na"]


class ColumnRule(BaseModel):
    """How one heatmap column is banded, read from the agent's config."""

    model_config = _FROZEN

    agent: str
    unit: Literal["z", "days"]
    direction: Literal["low", "high", "both"]
    # z: absolute z where warning starts; days: the value above which it is warning.
    warning_from: float
    # Critical strictly beyond this: the agent's own threshold (signed for z).
    critical_beyond: float
    rule: str  # what the agent tests, in words
    # The agent tests a site only with at least this many items (open queries); below it
    # the cell is too_few, whatever its value.
    min_count: int | None = None
    count_label: str | None = None  # what is counted, for example "open queries"


class SiteMetrics(BaseModel):
    model_config = _FROZEN

    site_id: str
    treated: int
    tested: bool
    ae_rate_z: float | None
    deviation_z: float | None
    median_entry_lag_days: float | None
    median_open_query_age_days: float | None
    open_queries: int | None
    csm_log_sd_z: dict[str, float | None]
    csm_sd_ratio: dict[str, float | None]
    # Per column key (ae_rate_z, deviation_z, median_entry_lag_days,
    # median_open_query_age_days, csm_log_sd_z:<PARAMETER>).
    bands: dict[str, Band]
    flagged_checks: list[str]
    flagged_by: list[SiteFlag]


class QtlView(BaseModel):
    """A study-level quality tolerance limit, as qtl_detector computes it."""

    model_config = _FROZEN

    name: str
    rate: float | None
    limit: float
    approach_from: float
    status: Literal["within", "approaching", "breached"] | None
    discontinued: int | None
    treated: int


class RbqmSites(BaseModel):
    model_config = _FROZEN

    study_id: str
    cut_id: str
    parameters: list[str]
    sites: list[SiteMetrics]
    qtl: QtlView
    # The rule behind each column's bands, from the agents' config.
    columns: dict[str, ColumnRule]


class AssetStatus(BaseModel):
    model_config = _FROZEN

    level: Literal["good", "warning", "critical", "info"]
    text: str


class AssetStatic(BaseModel):
    """Served as written in config/portfolio.toml; nothing here is computed."""

    model_config = _FROZEN

    title: str
    stage: Literal["preclinical", "phase_1", "phase_2", "phase_3"]
    indication: str
    intervention: str | None
    milestone: str
    milestone_due: str
    progress_percent: int
    probability_of_success: str | None
    status: AssetStatus


class AssetLive(BaseModel):
    """Computed from the ledger at request time, as the caller may see it."""

    model_config = _FROZEN

    open_findings: int


class PortfolioAsset(BaseModel):
    model_config = _FROZEN

    asset_id: str
    static: AssetStatic
    live: AssetLive | None


class Portfolio(BaseModel):
    model_config = _FROZEN

    assets: list[PortfolioAsset]


class SiteEnrollment(BaseModel):
    model_config = _FROZEN

    site_id: str
    cumulative: list[int]  # treated subjects up to and including each month


class Enrollment(BaseModel):
    """Cumulative treated subjects by month of first treatment (blinded DM RFSTDTC)."""

    model_config = _FROZEN

    study_id: str
    cut_id: str
    months: list[str]  # YYYY-MM, every month from the first to the last start
    total: list[int]
    sites: list[SiteEnrollment]


class ErrorBody(BaseModel):
    model_config = _FROZEN

    detail: str


# ---- SAE reconciliation (phase 3 step 3a)


class CheckCount(BaseModel):
    model_config = _FROZEN

    check: str
    count: int


class SiteCount(BaseModel):
    model_config = _FROZEN

    site_id: str
    count: int


class ReconciliationItem(BaseModel):
    """One discrepancy: a current safety.sae_reconciler finding the caller may see, with
    the two values it compares. subject_id is set only on subject-level findings, which
    only the roles in their visible_to ever receive."""

    model_config = _FROZEN

    finding_id: str
    check: str
    site_id: str | None
    subject_id: str | None
    severity: Severity
    status: FindingStatus
    title: str
    left_label: str
    left: str
    right_label: str
    right: str


class Reconciliation(BaseModel):
    model_config = _FROZEN

    study_id: str
    cut_id: str
    scope: CutScope
    agent_id: str
    agent_version: str | None  # the newest version that completed a run on the cut
    run_completed: bool
    by_check: list[CheckCount]  # every check, zero counts included
    by_site: list[SiteCount]
    items: list[ReconciliationItem]


class UnblindedCases(BaseModel):
    """How many safety cases on the operational cut carry an unblinded treatment field.
    Only the count: the treatment itself is never served."""

    model_config = _FROZEN

    cut_id: str
    cases: int
    cases_with_treatment: int
