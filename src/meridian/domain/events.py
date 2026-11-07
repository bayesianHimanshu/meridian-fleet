"""Ledger events: payloads, the sealed envelope, drafts, and redacted views."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from meridian.domain.enums import EventType, Visibility
from meridian.domain.findings import Decision, Finding
from meridian.domain.ids import (
    ActorId,
    AgentId,
    DedupKey,
    Sha256Digest,
    Sha256Hex,
    StudyId,
    UtcDatetime,
)

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
    # Null on events written before agent versions were part of identity.
    agent_version: str | None = None
    # Where the run executes ("inprocess" or "openshell"). Known before the run starts.
    # Null on events written before phase 2.
    executor: str | None = None


class ActorSuspended(BaseModel):
    model_config = _FROZEN

    type: Literal[EventType.ACTOR_SUSPENDED] = EventType.ACTOR_SUSPENDED
    agent_id: AgentId
    trigger_seq: PositiveInt
    # Null on events written before agent versions were part of identity.
    agent_version: str | None = None
    # Measured during the run, so recorded when it ends: the executor, the image the
    # sandbox ran (content digest) and the sha256 of the policy the sandbox reported as
    # effective. Null in process, and on events written before phase 2.
    executor: str | None = None
    image_digest: Sha256Digest | None = None
    policy_sha256: Sha256Hex | None = None
    # sha256 of the inputs_manifest.json the worker verified (phase 2 step 2). Null in
    # process and on older events.
    inputs_manifest_sha256: Sha256Hex | None = None


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
    # Phase 2 step 2b, all optional (null on older events). source says who observed the
    # refusal: "harness" (the exit code and stderr seen from outside the sandbox) or
    # "openshell_log" (the OpenShell supervisor's log line, stored verbatim in raw_line).
    source: Literal["harness", "openshell_log"] | None = None
    zone: str | None = None
    check: str | None = None
    sandbox: str | None = None
    policy_sha256: Sha256Hex | None = None
    raw_line: str | None = None


class ContainmentCheck(BaseModel):
    """One command of a conformance run and what happened to it."""

    model_config = _FROZEN

    name: Annotated[str, Field(min_length=1)]
    expect: Literal["refused", "allowed"]
    command: Annotated[str, Field(min_length=1)]
    outcome: Literal["refused", "allowed", "inconclusive"]
    exit_code: int | None
    message: str
    passed: bool


class ContainmentCheckCompleted(BaseModel):
    """A zone's committed policy was checked in a real sandbox (meridian sandbox conformance)."""

    model_config = _FROZEN

    type: Literal[EventType.CONTAINMENT_CHECK_COMPLETED] = EventType.CONTAINMENT_CHECK_COMPLETED
    zone: Annotated[str, Field(min_length=1)]
    sandbox: Annotated[str, Field(min_length=1)]
    image: Annotated[str, Field(min_length=1)]
    image_digest: Sha256Digest
    policy_sha256: Sha256Hex | None
    checks: tuple[ContainmentCheck, ...]
    log_line_found: bool
    # Every expected refusal was refused. passed adds the control and the log line.
    all_refused: bool
    passed: bool
    # Phase 2 step 3, null or empty on older events. policy_name is set when an agent has
    # its own policy (rbqm.narrator); providers were attached at create; and
    # credential_value_visible says whether ANTHROPIC_API_KEY inside the sandbox started
    # with "sk-ant-" (it must not). Only true or false is ever recorded, never the value.
    policy_name: str | None = None
    providers: tuple[str, ...] = ()
    credential_value_visible: bool | None = None


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
    | ContainmentCheckCompleted
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
