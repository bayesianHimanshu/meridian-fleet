"""Findings raised by agents and decisions humans make about them."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from meridian.domain.enums import DecisionAction, FindingKind, Role, Severity, Visibility
from meridian.domain.ids import ActorId, AgentId, FindingId, SiteId, StudyId

_FROZEN = ConfigDict(frozen=True, extra="forbid")


EvidenceRole = Literal["primary", "supporting"]
EvidenceDirection = Literal["low", "high", "none"]


class Evidence(BaseModel):
    """One measurable fact supporting a finding.

    Phase 2 step 4 adds context, all optional (null on events written before it): role
    says whether the finding rests on this item (primary) or it only adds context
    (supporting); direction is the value against its reference (low, high, or none for a
    p value or a count); expected is that reference (the expected count, the median site,
    the chance level of a terminal digit, the limit); display is the value as it should
    be read, at the precision fixed for its statistic (meridian.agents.rbqm.describe).
    """

    model_config = _FROZEN

    source: Annotated[str, Field(min_length=1)]
    selector: Annotated[str, Field(min_length=1)]
    statistic: Annotated[str, Field(min_length=1)]
    value: FiniteFloat
    threshold: FiniteFloat | None = None
    role: EvidenceRole | None = None
    direction: EvidenceDirection | None = None
    expected: FiniteFloat | None = None
    display: Annotated[str, Field(min_length=1)] | None = None


class Finding(BaseModel):
    """Something an agent believes a human should look at.

    finding_id is issued by the ledger when the finding is first recorded, so a
    finding inside an EventDraft carries finding_id=None.
    """

    model_config = _FROZEN

    finding_id: FindingId | None = None
    kind: FindingKind
    agent_id: AgentId
    agent_version: Annotated[str, Field(min_length=1)]
    study_id: StudyId
    site_id: SiteId | None = None
    severity: Severity
    title: Annotated[str, Field(min_length=1)]
    summary: Annotated[str, Field(min_length=1)]
    evidence: Annotated[tuple[Evidence, ...], Field(min_length=1)]
    visibility: Visibility
    visible_to: frozenset[Role]
    score: FiniteFloat


class Decision(BaseModel):
    """A human's disposition of a finding."""

    model_config = _FROZEN

    finding_id: FindingId
    action: DecisionAction
    actor: ActorId
    role: Role
    reason: str | None = None

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        if self.role is Role.QA_AUDITOR:
            raise ValueError("role qa_auditor may never make a decision")
        if self.action is DecisionAction.DISMISSED and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError("a non-blank reason is required when action is dismissed")
        return self
