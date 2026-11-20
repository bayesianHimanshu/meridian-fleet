"""Response and request models of the Fleet API (the OpenAPI contract with the UI)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.domain.enums import DecisionAction, EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import (
    AssessmentCandidate,
    AssessmentStatus,
    Event,
    GateDecision,
    GatePack,
    GateRequest,
    NarrativeStatus,
    PortfolioOutlook,
    RedactedEvent,
    SubstrateRun,
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


class PreviousDecision(BaseModel):
    """A person's decision (accepted or dismissed) on the finding an earlier version of the
    same agent raised with the same (cut, kind, site, check)."""

    model_config = _FROZEN

    finding_id: str
    agent_version: str
    action: DecisionAction
    actor: str
    role: Role
    reason: str | None
    recorded_at: datetime
    seq: int
    # Canonical hash of that finding's primary evidence display values.
    primary_evidence_sha256: str


class EvidenceChangedSince(BaseModel):
    """The primary evidence differs from the finding a person already closed: before and
    after are the display values, removed and added the difference."""

    model_config = _FROZEN

    decision: PreviousDecision
    before: list[str]
    after: list[str]
    removed: list[str]
    added: list[str]
    primary_evidence_sha256: str


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
    # Phase 4b step 1. previously_decided: a person already accepted or dismissed the same
    # finding from an earlier agent version and the primary evidence is unchanged; the
    # finding then stays out of the review queue and the default list. Otherwise, when the
    # evidence changed, evidence_changed_since says what changed since that decision.
    previously_decided: PreviousDecision | None = None
    evidence_changed_since: EvidenceChangedSince | None = None
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
    # Phase 3 step 4b. raised_by: the person who raised it (a QA observation), else null.
    # actor_under_review: the person whose own actions it is about, else null. may_decide:
    # whether the caller may record a decision on it; decide_blocked_reason says why not.
    raised_by: str | None = None
    actor_under_review: str | None = None
    may_decide: bool = False
    decide_blocked_reason: str | None = None


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
    # executor substrate (phase 4a): the Agent Substrate release, template and its spec
    # hash, WorkerPool, sandbox class, actor and the golden snapshot it was cloned from.
    substrate: SubstrateRun | None = None


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


class CorrectionView(BaseModel):
    """An AttributionCorrection of an event (phase 5 step 3b): the event stays as recorded;
    this says what it recorded, what is true and who corrected it."""

    model_config = _FROZEN

    seq: int
    corrects_seq: int
    field: str
    recorded_value: str
    true_value: str
    explanation: str
    corrected_by: str
    recorded_at: datetime


class LedgerPage(BaseModel):
    model_config = _FROZEN

    entries: list[Event | RedactedEvent]
    # Phase 5 step 3b: corrections of the attribution of events on this page, by seq.
    corrections: dict[int, list[CorrectionView]] = Field(default_factory=dict)
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
        Literal["completed", "running", "not_completed", "accepted", "rejected", "refused", "error"]
        | None
    )
    last_trigger_seq: int | None
    # Every version of the agent. A detector run failed when its ActorWoke has no
    # ActorSuspended and it started longer ago than the run timeout ([runtime]
    # exec_timeout_seconds); until then it is running, not failed. A narrator or
    # adjudicator run failed when it recorded status "error".
    runs_7d: int
    failed_7d: int
    last_failure_at: datetime | None
    # The ledger event types that record this agent's runs (for a filtered ledger link).
    run_event_types: list[EventType]
    # Where its runs go now ([runtime]): openshell or substrate (phase 4a), and for
    # substrate the placement of the last completed run of this version.
    executor: str = "openshell"
    substrate: SubstrateRun | None = None


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
    # openshell, or substrate (phase 4a: the check ran in Agent Substrate actors; then
    # applied_policy_sha256 is the zone template's spec hash).
    executor: str = "openshell"
    substrate: SubstrateRun | None = None


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


class NoaelBrief(BaseModel):
    """The current NOAEL decision for one sex (or combined), for the portfolio."""

    model_config = _FROZEN

    decision_id: str
    sex: Literal["M", "F", "combined"]
    established: bool
    noael_dose: float | None
    dose_unit: str
    recorded_at: datetime
    decided_by: str


class ChainLink(BaseModel):
    """One link of the hand-off from a study to a planned first human dose, with the ledger
    seq of the event that records it."""

    model_config = _FROZEN

    step: Literal["study", "noael", "proposal", "decision"]
    label: str
    ref: str  # the study id, the NOAEL decision id, or the finding id
    who: str | None
    seq: int


class StartingDose(BaseModel):
    """A preclinical asset's planned first-in-human starting dose (MRD-101's), or why there
    is none (phase 5 step 2; step 3a: it is the asset's own, not MRD-110's). planned:
    the medical monitor accepted the current proposal. not_shown: a role outside the
    proposal's roles (then nothing else is filled)."""

    model_config = _FROZEN

    status: Literal[
        "planned",
        "proposed",
        "escalated",
        "dismissed",
        "no_starting_dose",
        "no_proposal",
        "no_decision",
        "not_shown",
    ]
    dose_mg: float | None = None
    mrsd_mg_per_kg: float | None = None
    human_weight_kg: float | None = None
    why: str
    chain: list[ChainLink] = Field(default_factory=list)


class MonitoringBrief(BaseModel):
    """The current monitoring recommendation for a preclinical asset's first-in-human study
    (phase 5 step 3a): its status, its organs, who decided it."""

    model_config = _FROZEN

    status: Literal["none", "open", "accepted", "escalated", "dismissed", "not_shown"]
    finding_id: str | None = None
    organs: list[str] = Field(default_factory=list)
    unmapped: int = 0
    decided_by: str | None = None
    decided_seq: int | None = None


class AssetLive(BaseModel):
    """Computed from the ledger at request time, as the caller may see it."""

    model_config = _FROZEN

    open_findings: int
    # A preclinical asset (phase 5): its study's current NOAEL decisions. noael_status is
    # "not_shown" for a role that does not see preclinical data (noael is then empty).
    noael_status: Literal["decided", "not_yet_decided", "not_shown"] | None = None
    noael: list[NoaelBrief] = Field(default_factory=list)
    # Phase 5 step 2: the percent of first-in-human requirements met by the asset's
    # inventory (preclinical roles), and the planned starting dose of its first human
    # trial with its chain (the dose proposal's roles).
    ind_status: Literal["assessed", "not_loaded", "not_shown"] | None = None
    ind_readiness_percent: float | None = None
    starting_dose: StartingDose | None = None
    # Phase 5 step 3a: the first-in-human study's monitoring recommendation.
    monitoring: MonitoringBrief | None = None


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


# ---- TMF completeness (phase 3 step 4a)

TmfCellStatus = Literal["filed", "filed_late", "missing", "not_expected"]


class TmfArtifact(BaseModel):
    """One expected artifact of config/tmf_expected.toml (names only, no numbers)."""

    model_config = _FROZEN

    key: str
    name: str
    zone: str
    level: Literal["trial", "site"]
    milestone: str
    file_within_days: int
    essential: bool


class TmfCell(BaseModel):
    """One artifact at one site (or the trial). For a visit report, expected counts the
    visits that are due, filed those with a report, late those filed late."""

    model_config = _FROZEN

    artifact: str
    status: TmfCellStatus
    expected: int
    filed: int
    late: int
    finding_ids: list[str]  # current findings about this cell that the caller may see


class TmfSiteRow(BaseModel):
    model_config = _FROZEN

    site_id: str
    completeness_percent: float | None
    timely_percent: float | None
    cells: list[TmfCell]  # one per site-level artifact, in the order of artifacts


class TmfFindingItem(BaseModel):
    model_config = _FROZEN

    finding_id: str
    check: str
    site_id: str | None
    severity: Severity
    status: FindingStatus
    title: str


class TmfCompletenessView(BaseModel):
    """The TMF of one cut: completeness and timely filing per site and for the trial, the
    expected-against-filed matrix (computed from the cut's eTMF index), and the current
    quality.tmf_completeness findings the caller may see. No subject data."""

    model_config = _FROZEN

    study_id: str
    cut_id: str
    scope: CutScope
    export_date: str | None  # ISO date of the eTMF export
    agent_id: str
    agent_version: str | None
    run_completed: bool
    completeness_percent: float | None  # everything expected, trial and sites
    timely_percent: float | None
    trial_completeness_percent: float | None
    trial_timely_percent: float | None
    artifacts: list[TmfArtifact]
    trial: list[TmfCell]  # one per trial-level artifact
    sites: list[TmfSiteRow]
    findings: list[TmfFindingItem]


# ---- Quality observations and the audit trail review (phase 3 step 4b)


class ObservationRequest(BaseModel):
    model_config = _FROZEN

    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=4000)]
    severity: Severity
    finding_id: Annotated[str, Field(pattern=r"^[A-Z]+-[0-9]{4}$")] | None = None
    ledger_seq: Annotated[int, Field(ge=1)] | None = None


class ObservationResponse(BaseModel):
    model_config = _FROZEN

    observation_id: str
    observation_seq: int
    finding_id: str
    finding_seq: int


class ObservationItem(BaseModel):
    model_config = _FROZEN

    finding_id: str
    seq: int
    recorded_at: datetime
    raised_by: str
    severity: Severity
    status: FindingStatus
    title: str
    refers_to_finding_id: str | None
    refers_to_seq: int | None


class AuditFindingItem(BaseModel):
    model_config = _FROZEN

    finding_id: str
    check: str
    severity: Severity
    status: FindingStatus
    title: str
    actor_under_review: str | None  # the person whose decisions it is about, if any


class AuditView(BaseModel):
    """The Audit tab: the last audit review and the current audit findings the caller may
    see. chain_linkage: intact, broken, or not_reviewed (no completed review yet)."""

    model_config = _FROZEN

    agent_id: str
    agent_version: str | None
    last_review_seq: int | None  # the ActorWoke of the last review
    last_review_at: datetime | None
    review_from_seq: int | None
    review_to_seq: int | None
    review_completed: bool
    chain_linkage: Literal["intact", "broken", "not_reviewed"]
    chain_findings: int
    by_check: list[CheckCount]
    findings: list[AuditFindingItem]


# ---- preclinical (phase 5 step 1)


class DoseGroupView(BaseModel):
    model_config = _FROZEN

    setcd: str
    phase: Literal["main", "recovery"]
    dose: float
    control: bool
    males: int
    females: int


class PreclinicalStudy(BaseModel):
    model_config = _FROZEN

    study_id: str
    asset_id: str | None
    cut_id: str
    loaded_seq: int
    title: str
    species: str
    strain: str
    route: str
    duration: str
    dose_unit: str
    groups: list[DoseGroupView]


class GradeCount(BaseModel):
    model_config = _FROZEN

    grade: int
    animals: int


class EndpointGroupView(BaseModel):
    model_config = _FROZEN

    rank: int
    dose: float
    n: int
    mean: float | None = None
    sd: float | None = None
    percent_vs_control: float | None = None
    p: float | None = None
    affected: int | None = None
    grades: list[GradeCount] = Field(default_factory=list)
    flagged: bool = False


class EndpointTableView(BaseModel):
    model_config = _FROZEN

    endpoint: str
    label: str
    kind: Literal["continuous", "incidence"]
    unit: str
    sex: Literal["M", "F"]
    phase: Literal["main", "recovery"]
    groups: list[EndpointGroupView]
    trend_p: float | None
    check: str | None
    finding_id: str | None


class DoseSupport(BaseModel):
    """Decision support for the NOAEL: per dose group, the findings that flag it."""

    model_config = _FROZEN

    phase: Literal["main", "recovery"]
    dose: float
    dose_related: list[str]
    without_trend: list[str]


class PreclinicalEndpoints(BaseModel):
    model_config = _FROZEN

    study_id: str
    cut_id: str
    dose_unit: str
    tables: list[EndpointTableView]
    by_dose: list[DoseSupport]


# Phase 5 step 2: the API's minimum for a NOAEL rationale, after trimming. The event keeps
# its own lower minimum (20) so that decisions recorded before this step still read.
NOAEL_RATIONALE_MIN = 200


class NoaelRequest(BaseModel):
    model_config = _FROZEN

    sex: Literal["M", "F", "combined"]
    established: bool
    noael_dose: Annotated[float, Field(ge=0)] | None = None
    loael_dose: Annotated[float, Field(ge=0)] | None = None
    finding_ids: list[Annotated[str, Field(pattern=r"^[A-Z]+-[0-9]{4}$")]] = Field(
        default_factory=list, max_length=200
    )
    rationale: Annotated[str, Field(min_length=20, max_length=4000)]
    supersedes: Annotated[str, Field(pattern=r"^NOAEL-[0-9a-f]{12}$")] | None = None


class NoaelDecisionView(BaseModel):
    model_config = _FROZEN

    seq: int
    recorded_at: datetime
    decision_id: str
    study_id: str
    sex: Literal["M", "F", "combined"]
    established: bool
    noael_dose: float | None
    loael_dose: float | None
    dose_unit: str
    finding_ids: list[str]
    rationale: str
    decided_by: str
    supersedes: str | None
    superseded_by: str | None
    current: bool


class NoaelCandidate(BaseModel):
    """A current finding of the study the NOAEL may rest on, with what the form groups it
    by: its sex (None when it has none, for example a data-quality finding) and the
    treated doses where it flags a group, per phase."""

    model_config = _FROZEN

    finding_id: str
    title: str
    status: str
    sex: Literal["M", "F"] | None
    main_doses: list[float]
    recovery_doses: list[float]


class NoaelHistory(BaseModel):
    model_config = _FROZEN

    study_id: str
    dose_unit: str
    doses: list[float]
    current: list[NoaelDecisionView]
    history: list[NoaelDecisionView]
    may_record: bool
    # Phase 5 step 2: what the form needs to apply the API's own rules before submit.
    rationale_min_chars: int = NOAEL_RATIONALE_MIN
    candidates: list[NoaelCandidate] = Field(default_factory=list)


class DoseStep(BaseModel):
    """One evidence item of a dose proposal: a step of the working, its source, its value."""

    model_config = _FROZEN

    statistic: str
    role: str | None
    value: float
    source: str
    selector: str
    display: str


class ProposalView(BaseModel):
    model_config = _FROZEN

    finding_id: str
    seq: int
    # Derived at read time (phase 5 step 3a); recorded_title is the text in the ledger.
    title: str
    recorded_title: str
    summary: str
    check: str
    status: str
    basis_decision_id: str | None
    steps: list[DoseStep]
    decided_by: str | None
    decided_seq: int | None
    decision_reason: str | None


class DoseTranslationView(BaseModel):
    """The Dose translation panel (phase 5 step 2): the current proposal and its working,
    or why there is none."""

    model_config = _FROZEN

    study_id: str
    status: Literal["proposed", "no_starting_dose", "no_decision", "not_run"]
    why: str
    current: ProposalView | None
    superseded: list[str]


class RequirementView(BaseModel):
    model_config = _FROZEN

    id: str
    title: str
    source: str
    section: str
    quote: str
    note: str
    status: Literal["met", "not_met", "not_applicable"]
    required_weeks: float | None
    candidates: int
    study_id: str | None
    study_title: str | None
    species: str | None
    duration_weeks: float | None
    glp: str | None
    report_status: str | None
    qa_audit_date: str | None
    problems: list[str]
    finding_ids: list[str]


class IndReadinessView(BaseModel):
    """The IND readiness checklist (phase 5 step 2): every requirement with its status,
    the study that meets it (or not) and links to the gap findings. The percentage is a
    summary, not a finding."""

    model_config = _FROZEN

    asset_id: str
    status: Literal["assessed", "not_loaded"]
    cut_id: str | None
    loaded_seq: int | None
    planned_submission_date: str
    planned_clinical_duration_days: int
    multiple_dose: bool
    percent_met: float | None
    met: int
    applicable: int
    requirements: list[RequirementView]


class MonitoringSourceView(BaseModel):
    model_config = _FROZEN

    source: str
    section: str
    quote: str


class MonitoringItemView(BaseModel):
    model_config = _FROZEN

    organ: str
    label: str
    measure: str
    frequency: str
    review: str
    stopping: str
    sources: list[MonitoringSourceView]
    findings: list[str]
    finding_titles: list[str]


class MarginView(BaseModel):
    model_config = _FROZEN

    label: Literal["NOAEL", "LOAEL"]
    hed_mg: float
    human_weight_kg: float | None
    display: str
    ratio: float | None


class RecommendationView(BaseModel):
    model_config = _FROZEN

    finding_id: str
    seq: int
    title: str
    status: str
    decided_by: str | None
    decided_seq: int | None
    items: list[MonitoringItemView]
    unmapped: list[str]
    margins: list[MarginView]
    starting_dose: str
    basis: str


class TranslationalView(BaseModel):
    """The Translational panel (phase 5 step 3a): the current monitoring recommendation for
    the first-in-human study, its sources and the safety margins, or why there is none."""

    model_config = _FROZEN

    study_id: str
    status: Literal["recommended", "no_decision", "not_run"]
    why: str
    current: RecommendationView | None
    superseded: list[str]


class SignalRow(BaseModel):
    model_config = _FROZEN

    term: str
    reports: int  # a: reports with the drug and the term
    drug_reports: int
    term_reports: int  # a + c
    prr: float | None
    prr_low: float | None
    prr_high: float | None
    ror: float | None
    ror_low: float | None
    ror_high: float | None
    chi2: float
    signal: bool
    label_listed: bool | None
    finding_id: str | None
    status: str | None
    # 1.0.1 triage (docs/signal-scout.md): listed, indication, use, weak, priority or
    # not_signal. Only priority signals are findings in the review queue.
    tier: str = "not_signal"


class ScanSummary(BaseModel):
    """One completed signal scan: who asked for it (as recorded, with any correction), the
    agent version, and its findings."""

    model_config = _FROZEN

    scan_seq: int
    scan_id: str
    agent_version: str | None
    requested_by: str
    reuses_responses_of: int | None
    findings: int
    open: int
    corrections: list[CorrectionView]


class LabelReaction(BaseModel):
    model_config = _FROZEN

    reaction: str
    signal_terms: list[str]  # signal terms whose words name it (exact phrase)


class SafetySignalsView(BaseModel):
    """The Safety signals view (phase 5 step 3a): the latest scan of openFDA for MRD-201's
    drug, every tested term with its disproportionality, the signals, and the comparison
    with the US label. Reporting signals, not incidences or causes."""

    model_config = _FROZEN

    study_id: str
    drug: str
    status: Literal["scanned", "not_scanned", "responses_missing"]
    scan_id: str | None
    scan_seq: int | None
    data_date: str | None
    product_query: str
    drug_reports: int | None
    all_reports: int | None
    terms_with_drug: int | None
    tested: int
    # Terms with at least min_reports reports whose total query openFDA did not answer
    # (for example "Too Many Requests"): not tested, never silently dropped.
    not_answered: list[str] = Field(default_factory=list)
    signals: list[SignalRow]
    not_signals: list[SignalRow]
    label_set_id: str | None
    label_version: str | None
    label_effective: str | None
    signals_listed: int
    label_most_common: list[LabelReaction]
    rule: str
    caveats: list[str]
    caveats_source: str
    # Phase 5 step 3b: the shown scan as recorded and corrected, the scans before it, the
    # tiers and the context every signal carries.
    scan: ScanSummary | None = None
    earlier_scans: list[ScanSummary] = Field(default_factory=list)
    tiers: dict[str, int] = Field(default_factory=dict)
    triage: str = ""
    context: list[str] = Field(default_factory=list)
    requests: int


# ---- the portfolio's reviews and gates (phase 5 step 3b)


class ReviewRequest(BaseModel):
    model_config = _FROZEN

    note: Annotated[str, Field(max_length=500)] | None = None


class GateRequestBody(BaseModel):
    model_config = _FROZEN

    asset_id: Annotated[str, Field(min_length=1, max_length=32)]
    gate: Annotated[str, Field(pattern=r"^[a-z_]{1,64}$")]
    note: Annotated[str, Field(max_length=500)] | None = None


class GateDecisionBody(BaseModel):
    model_config = _FROZEN

    pack_seq: int
    decision: Literal["go", "conditional_go", "no_go"]
    conditions: list[Annotated[str, Field(min_length=3, max_length=500)]] = Field(
        default_factory=list
    )
    rationale: Annotated[str, Field(max_length=4000)]


class Recorded(BaseModel):
    """What the API recorded: the event's seq and its id."""

    model_config = _FROZEN

    seq: int
    id: str


class OutlookView(BaseModel):
    """The latest PortfolioOutlook, as recorded, with its seq. Information only."""

    model_config = _FROZEN

    status: Literal["outlook", "none"]
    seq: int | None = None
    recorded_at: datetime | None = None
    outlook: PortfolioOutlook | None = None
    # Reviews asked for that have no outlook yet (run the agents to answer them).
    pending_reviews: list[int] = Field(default_factory=list)
    narrated: bool = False  # no narrator in this step


class GateCriterionDef(BaseModel):
    model_config = _FROZEN

    id: str
    title: str
    basis: Literal["regulation", "policy"]
    source: str | None
    section: str | None


class GateDef(BaseModel):
    model_config = _FROZEN

    asset_id: str
    gate: str
    title: str
    criteria: list[GateCriterionDef]


class GateDecisionView(BaseModel):
    model_config = _FROZEN

    seq: int
    recorded_at: datetime
    decision: GateDecision


class GateRequestView(BaseModel):
    model_config = _FROZEN

    seq: int
    recorded_at: datetime
    request: GateRequest
    pack_seq: int | None
    pack: GatePack | None
    decision: GateDecisionView | None


class GatesView(BaseModel):
    """The gates of config/gates.toml, and every gate request with its evidence pack and
    decision (newest first). The agent never says go or no-go; the portfolio lead does."""

    model_config = _FROZEN

    gates: list[GateDef]
    requests: list[GateRequestView]
    can_request: bool  # the caller is the portfolio lead
