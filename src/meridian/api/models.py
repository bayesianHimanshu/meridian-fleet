"""Response and request models of the Fleet API (the OpenAPI contract with the UI)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.domain.enums import DecisionAction, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event, NarrativeStatus, RedactedEvent
from meridian.domain.findings import Evidence

_FROZEN = ConfigDict(frozen=True, extra="forbid")

FindingStatus = Literal["open", "accepted", "escalated", "dismissed"]
CutKind = Literal["base", "null", "faulted", "holdout", "sweep"]


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
    created: datetime
    agent_runs_completed: int


class LatestNarrative(BaseModel):
    model_config = _FROZEN

    narrator_version: str
    headline: str
    explanation: str
    seq: int


class FindingSummary(BaseModel):
    model_config = _FROZEN

    finding_id: str
    seq: int
    raised_at: datetime
    study_id: str
    cut_id: str
    check: str
    agent_id: str
    agent_version: str
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
    policy_sha256: str | None = None
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
    # Pass as ?since= to continue; None when this page reached the end.
    next_since: int | None


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
    role: Literal["detector", "narrator"]
    enabled: bool
    policy_file: str
    policy_sha256: str
    last_run_at: datetime | None
    last_outcome: (
        Literal["completed", "not_completed", "accepted", "rejected", "refused", "error"] | None
    )
    last_trigger_seq: int | None


class ContainmentView(BaseModel):
    model_config = _FROZEN

    policy: str
    zone: str
    seq: int
    recorded_at: datetime
    sandbox: str
    image_digest: str
    policy_sha256: str | None
    passed: bool
    all_refused: bool
    log_line_found: bool
    credential_value_visible: bool | None


class Fleet(BaseModel):
    model_config = _FROZEN

    agents: list[FleetAgent]
    containment: list[ContainmentView]


class SiteMetrics(BaseModel):
    model_config = _FROZEN

    site_id: str
    treated: int
    tested: bool
    ae_rate_z: float | None
    deviation_z: float | None
    median_entry_lag_days: float | None
    median_open_query_age_days: float | None
    csm_log_sd_z: dict[str, float | None]
    flagged_checks: list[str]


class RbqmSites(BaseModel):
    model_config = _FROZEN

    study_id: str
    cut_id: str
    parameters: list[str]
    sites: list[SiteMetrics]


class AssetStatic(BaseModel):
    """Served as written in config/portfolio.toml."""

    model_config = _FROZEN

    name: str
    stage: str
    indication: str
    modality: str


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


class ErrorBody(BaseModel):
    model_config = _FROZEN

    detail: str
