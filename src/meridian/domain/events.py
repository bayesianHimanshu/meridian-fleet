"""Ledger events: payloads, the sealed envelope, drafts, and redacted views."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from meridian.domain.enums import EventType, Visibility
from meridian.domain.findings import Decision, Finding
from meridian.domain.ids import ActorId, AgentId, DedupKey, Sha256Hex, StudyId, UtcDatetime

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class DataCut(BaseModel):
    model_config = _FROZEN

    type: Literal[EventType.DATA_CUT] = EventType.DATA_CUT
    cut_id: Annotated[str, Field(min_length=1)]
    study_id: StudyId
    path: Annotated[str, Field(min_length=1)]


class DocumentFiled(BaseModel):
    model_config = _FROZEN

    type: Literal[EventType.DOCUMENT_FILED] = EventType.DOCUMENT_FILED
    doc_id: Annotated[str, Field(min_length=1)]
    study_id: StudyId
    zone: Annotated[str, Field(min_length=1)]
    artifact: Annotated[str, Field(min_length=1)]


class ActorWoke(BaseModel):
    model_config = _FROZEN

    type: Literal[EventType.ACTOR_WOKE] = EventType.ACTOR_WOKE
    agent_id: AgentId
    trigger_seq: PositiveInt


class ActorSuspended(BaseModel):
    model_config = _FROZEN

    type: Literal[EventType.ACTOR_SUSPENDED] = EventType.ACTOR_SUSPENDED
    agent_id: AgentId
    trigger_seq: PositiveInt


class FindingRaised(BaseModel):
    model_config = _FROZEN

    type: Literal[EventType.FINDING_RAISED] = EventType.FINDING_RAISED
    finding: Finding
    dedup_key: DedupKey


class PolicyDenied(BaseModel):
    model_config = _FROZEN

    type: Literal[EventType.POLICY_DENIED] = EventType.POLICY_DENIED
    agent_id: AgentId
    resource: Annotated[str, Field(min_length=1)]
    policy: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]


class HumanDecision(BaseModel):
    model_config = _FROZEN

    type: Literal[EventType.HUMAN_DECISION] = EventType.HUMAN_DECISION
    decision: Decision


EventPayload = Annotated[
    DataCut
    | DocumentFiled
    | ActorWoke
    | ActorSuspended
    | FindingRaised
    | PolicyDenied
    | HumanDecision,
    Field(discriminator="type"),
]


def _check_visibility(visibility: Visibility, payload: EventPayload) -> None:
    if (
        isinstance(payload, FindingRaised)
        and payload.finding.visibility is Visibility.RESTRICTED
        and visibility is not Visibility.RESTRICTED
    ):
        raise ValueError("an event carrying a restricted finding must itself be restricted")


class EventDraft(BaseModel):
    """What a caller hands the ledger. The ledger assigns everything else."""

    model_config = _FROZEN

    occurred_at: UtcDatetime
    actor: ActorId
    study_id: StudyId | None = None
    visibility: Visibility
    payload: EventPayload

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_visibility(self.visibility, self.payload)
        return self


class Event(BaseModel):
    """A sealed, hash-chained ledger entry."""

    model_config = _FROZEN

    seq: PositiveInt
    event_id: UUID
    occurred_at: UtcDatetime
    recorded_at: UtcDatetime
    actor: ActorId
    study_id: StudyId | None = None
    visibility: Visibility
    payload: EventPayload
    prev_hash: Sha256Hex
    hash: Sha256Hex

    @property
    def event_type(self) -> EventType:
        return self.payload.type

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_visibility(self.visibility, self.payload)
        return self


class RedactedEvent(BaseModel):
    """What a reader without unblinded access sees in place of a restricted event."""

    model_config = _FROZEN

    seq: PositiveInt
    event_type: EventType
    recorded_at: UtcDatetime
    hash: Sha256Hex
    prev_hash: Sha256Hex
    marker: Literal["restricted"] = "restricted"


LedgerEntry = Event | RedactedEvent
