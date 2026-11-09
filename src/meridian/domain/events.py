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
    FindingId,
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
    # The committed policy file passed to `openshell sandbox create` (its path, and the
    # sha256 of its bytes, comments included). policy_sha256 above is the applied policy:
    # what the sandbox reports back, without comments, so policies that differ only in
    # their header comment share it. Null in process and before step 3a polish.
    policy_file: str | None = None
    policy_file_sha256: Sha256Hex | None = None


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


NarrativeStatus = Literal["accepted", "rejected", "refused", "error"]


class FindingNarrative(BaseModel):
    """A plain-language explanation of one finding, by rbqm.narrator (phase 2 step 3).

    The finding itself is never changed. accepted: the model's text passed validation, and
    only then are headline and explanation recorded. rejected: it failed validation
    (reasons say why). refused: the de-identification check stopped the input; nothing
    was sent. error: the call failed (retried on the next pass). Hashes are of the
    canonical request body sent and of the raw response body received.
    """

    model_config = _FROZEN

    type: Literal[EventType.FINDING_NARRATIVE] = EventType.FINDING_NARRATIVE
    finding_id: FindingId
    finding_seq: PositiveInt
    agent_id: AgentId
    agent_version: Annotated[str, Field(min_length=1)]
    status: NarrativeStatus
    headline: str | None = None
    explanation: str | None = None
    model: Annotated[str, Field(min_length=1)]
    request_sha256: Sha256Hex | None = None
    response_sha256: Sha256Hex | None = None
    rejection_reasons: tuple[str, ...] = ()
    error: str | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    latency_ms: Annotated[int, Field(ge=0)] | None = None
    executor: str | None = None
    sandbox: str | None = None
    policy_sha256: Sha256Hex | None = None
    # The digest Ollama served the model with, for a local model (phase 3 step 3b:
    # safety.local_narrator). Null for the cloud narrator and on older events.
    model_digest: Sha256Hex | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        text = (self.headline, self.explanation)
        if self.status == "accepted":
            if not all(text):
                raise ValueError("an accepted narrative needs a headline and an explanation")
            if self.rejection_reasons:
                raise ValueError("an accepted narrative has no rejection reasons")
        elif any(t is not None for t in text):
            raise ValueError(f"a {self.status} narrative records no headline or explanation")
        if self.status in ("rejected", "refused") and not self.rejection_reasons:
            raise ValueError(f"a {self.status} narrative needs its reasons")
        if self.status == "error" and not self.error:
            raise ValueError("an error narrative needs the error")
        if self.status != "refused" and self.request_sha256 is None:
            raise ValueError("a narrative that was sent needs the request sha256")
        if self.status == "refused" and self.request_sha256 is not None:
            raise ValueError("a refused narrative sent nothing")
        return self


AssessmentStatus = Literal["accepted", "rejected", "error"]
Verdict = Literal["yes", "no", "unsure"]
AssessmentCandidate = Literal["term_mismatch", "unpaired_pair"]


class FindingAssessment(BaseModel):
    """A model's judgement about one or two findings, by safety.term_adjudicator (phase 3
    step 3b): do two event terms describe the same event?

    It never changes, closes or supersedes a finding; a person reads it next to them.
    candidate: term_mismatch (one sae_term_mismatch finding) or unpaired_pair (an
    sae_missing_in_safety_db and a case_without_clinical_sae finding of the same subject
    with onsets within the reconciler's window). accepted: the output passed validation,
    and only then are verdict and reason recorded. rejected: it failed validation
    (reasons say why); nothing of it is shown. error: the call failed or the served model
    was not the pinned one (retried on the next pass). Hashes are of the canonical
    request body sent and of the raw response body received; model_digest is the digest
    Ollama served.
    """

    model_config = _FROZEN

    type: Literal[EventType.FINDING_ASSESSMENT] = EventType.FINDING_ASSESSMENT
    finding_ids: Annotated[tuple[FindingId, ...], Field(min_length=1, max_length=2)]
    finding_seqs: Annotated[tuple[PositiveInt, ...], Field(min_length=1, max_length=2)]
    candidate: AssessmentCandidate
    agent_id: AgentId
    agent_version: Annotated[str, Field(min_length=1)]
    status: AssessmentStatus
    verdict: Verdict | None = None
    reason: str | None = None
    model: Annotated[str, Field(min_length=1)]
    model_digest: Sha256Hex | None = None
    request_sha256: Sha256Hex
    response_sha256: Sha256Hex | None = None
    rejection_reasons: tuple[str, ...] = ()
    error: str | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    latency_ms: Annotated[int, Field(ge=0)] | None = None
    executor: str | None = None
    sandbox: str | None = None
    policy_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if len(self.finding_ids) != len(self.finding_seqs):
            raise ValueError("one finding seq per finding id")
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ValueError("an assessment names each finding once")
        if len(self.finding_ids) != (1 if self.candidate == "term_mismatch" else 2):
            raise ValueError(f"a {self.candidate} assessment has the wrong number of findings")
        if self.status == "accepted":
            if self.verdict is None or not self.reason:
                raise ValueError("an accepted assessment needs a verdict and a reason")
            if self.rejection_reasons or self.model_digest is None:
                raise ValueError("an accepted assessment has a digest and no rejection reasons")
        elif self.verdict is not None or self.reason is not None:
            raise ValueError(f"a {self.status} assessment records no verdict or reason")
        if self.status == "rejected" and not self.rejection_reasons:
            raise ValueError("a rejected assessment needs its reasons")
        if self.status == "error" and not self.error:
            raise ValueError("an error assessment needs the error")
        return self


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
    | FindingNarrative
    | FindingAssessment
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
