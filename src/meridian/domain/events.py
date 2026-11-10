"""Ledger events: payloads, the sealed envelope, drafts, and redacted views."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from meridian.domain.enums import EventType, Severity, Visibility
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


class SubstrateRun(BaseModel):
    """Where an Agent Substrate run happened (phase 4a step 2).

    On ActorWoke it is the plan: the actor is named but not yet created (actor_uid and
    worker_pod are null). On ActorSuspended it is what the control plane reported for the
    actor just before it was deleted. The run never suspends: the only snapshot an actor
    has is the golden snapshot it was created from (golden_snapshot)."""

    model_config = _FROZEN

    version: Annotated[str, Field(min_length=1)]  # the release, for example v1.0.0
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    atespace: Annotated[str, Field(min_length=1)]
    template: Annotated[str, Field(min_length=1)]
    # sha256 of the template's canonical spec as ate-api reports it (status and server
    # bookkeeping dropped; meridian.sandbox.substrate_templates.spec_sha256).
    template_sha256: Sha256Hex
    # The committed template file and the sha256 of its bytes.
    template_file: Annotated[str, Field(min_length=1)]
    template_file_sha256: Sha256Hex
    image: Annotated[str, Field(pattern=r"^[^@\s]+@sha256:[0-9a-f]{64}$")]  # registry ref
    worker_pool: Annotated[str, Field(min_length=1)]
    sandbox_class: Annotated[str, Field(min_length=1)]
    actor: Annotated[str, Field(min_length=1)]
    golden_tag: Annotated[str, Field(min_length=1)]
    golden_snapshot: Annotated[str, Field(min_length=1)]  # the snapshot uuid of that tag
    actor_uid: str | None = None
    worker_pod: str | None = None


class ActorWoke(BaseModel):
    model_config = _FROZEN

    type: Literal[EventType.ACTOR_WOKE] = EventType.ACTOR_WOKE
    agent_id: AgentId
    trigger_seq: PositiveInt
    # Null on events written before agent versions were part of identity.
    agent_version: str | None = None
    # Where the run executes ("inprocess", "openshell" or "substrate"). Known before the run starts.
    # Null on events written before phase 2.
    executor: str | None = None
    # An audit review (phase 3 step 4b): the ledger seq range the run reviews. Null for a
    # run on a data cut.
    review_from_seq: PositiveInt | None = None
    review_to_seq: PositiveInt | None = None
    # executor "substrate" (phase 4a step 2): the planned placement. Null otherwise.
    substrate: SubstrateRun | None = None

    @model_validator(mode="after")
    def _range(self) -> Self:
        if (self.review_from_seq is None) != (self.review_to_seq is None):
            raise ValueError("a review range needs both ends")
        if self.review_from_seq is not None and self.review_to_seq is not None:
            if self.review_from_seq > self.review_to_seq:
                raise ValueError("review_from_seq may not exceed review_to_seq")
            if self.review_to_seq >= self.trigger_seq:
                raise ValueError("a review covers only events before its trigger")
        return self


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
    # executor "substrate" (phase 4a step 2): where the run happened. Null otherwise.
    substrate: SubstrateRun | None = None


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
    # "substrate_log" (phase 4a): an atenet-egress log line naming the refused target.
    # "runner_guard" (phase 4a step 3): the runner's own client refused a kubectl ate verb
    # outside its allow-list before sending it. "substrate_authz": ate-api-server answered
    # PermissionDenied to the runner's identity (raw_line holds its text).
    source: (
        Literal["harness", "openshell_log", "substrate_log", "runner_guard", "substrate_authz"]
        | None
    ) = None
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
    # Phase 4a: "substrate" for a check in Agent Substrate actors (null: OpenShell), with
    # the conformance actor's placement. policy_sha256 is then the zone template's spec.
    executor: str | None = None
    substrate: SubstrateRun | None = None


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
MAX_ANSWER_TEXT = 4000


def agreed_verdict(verdicts: tuple[Verdict | None, ...]) -> Verdict:
    """The agreement rule of safety.term_adjudicator 1.1.0: "yes" only if every model said
    yes, "no" only if every model said no, otherwise "unsure". A missing verdict (an
    answer that failed validation) agrees with nothing."""
    if verdicts and all(v == "yes" for v in verdicts):
        return "yes"
    if verdicts and all(v == "no" for v in verdicts):
        return "no"
    return "unsure"


class ModelAnswer(BaseModel):
    """One model's answer inside a FindingAssessment of the agreement rule (1.1.0).

    text is the raw response content as received (never a reasoning trace), recorded
    whether or not it passed validation; verdict and reason only when it did. An error
    answer has no text (the call failed, or the served digest was not the pinned one).
    """

    model_config = _FROZEN

    model: Annotated[str, Field(min_length=1)]
    model_digest: Sha256Hex | None = None
    status: AssessmentStatus
    verdict: Verdict | None = None
    reason: str | None = None
    text: Annotated[str, Field(max_length=MAX_ANSWER_TEXT)] | None = None
    rejection_reasons: tuple[str, ...] = ()
    error: str | None = None
    request_sha256: Sha256Hex
    response_sha256: Sha256Hex | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    latency_ms: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.status == "accepted":
            if self.verdict is None or not self.reason or self.model_digest is None:
                raise ValueError("an accepted answer needs a verdict, a reason and a digest")
            if self.rejection_reasons:
                raise ValueError("an accepted answer has no rejection reasons")
        elif self.verdict is not None or self.reason is not None:
            raise ValueError(f"a {self.status} answer records no verdict or reason")
        if self.status == "rejected" and not self.rejection_reasons:
            raise ValueError("a rejected answer needs its reasons")
        if self.status == "error" and (not self.error or self.text is not None):
            raise ValueError("an error answer needs the error and has no text")
        if self.status != "error" and (self.text is None or self.response_sha256 is None):
            raise ValueError("an answer that was received keeps its text and hash")
        return self


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

    Agreement (1.1.0): answers holds one ModelAnswer per model asked, each with its raw
    text, model and digest; model names the models joined by "+", and model_digest and
    request_sha256 are left out (each answer has its own). Then the status is error if
    any answer is an error, rejected if every answer was rejected, and accepted
    otherwise, with the verdict of agreed_verdict and a reason the host writes.
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
    # Required for a single-model assessment (1.0.0); left out with answers.
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
    # Agreement (1.1.0): one answer per model asked, in the order asked.
    answers: tuple[ModelAnswer, ...] = ()

    def _check_answers(self) -> None:
        if len(self.answers) < 2 or len({a.model for a in self.answers}) != len(self.answers):
            raise ValueError("an agreement assessment has two or more answers, one per model")
        if self.model != "+".join(a.model for a in self.answers):
            raise ValueError("an agreement assessment's model names its answers' models")
        if self.model_digest is not None or self.request_sha256 is not None:
            raise ValueError("an agreement assessment keeps digests and hashes per answer")
        statuses = {a.status for a in self.answers}
        expected = (
            "error"
            if "error" in statuses
            else "rejected"
            if statuses == {"rejected"}
            else "accepted"
        )
        if self.status != expected:
            raise ValueError(f"answers {sorted(statuses)} make the assessment {expected}")
        if self.status == "accepted" and self.verdict != agreed_verdict(
            tuple(a.verdict for a in self.answers)
        ):
            raise ValueError("the verdict of an agreement assessment is the agreed verdict")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.answers:
            self._check_answers()
        elif self.request_sha256 is None:
            raise ValueError("a single-model assessment needs the request sha256")
        if len(self.finding_ids) != len(self.finding_seqs):
            raise ValueError("one finding seq per finding id")
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ValueError("an assessment names each finding once")
        if len(self.finding_ids) != (1 if self.candidate == "term_mismatch" else 2):
            raise ValueError(f"a {self.candidate} assessment has the wrong number of findings")
        if self.status == "accepted":
            if self.verdict is None or not self.reason:
                raise ValueError("an accepted assessment needs a verdict and a reason")
            if self.rejection_reasons or (self.model_digest is None and not self.answers):
                raise ValueError("an accepted assessment has a digest and no rejection reasons")
        elif self.verdict is not None or self.reason is not None:
            raise ValueError(f"a {self.status} assessment records no verdict or reason")
        if self.status == "rejected" and not self.rejection_reasons:
            raise ValueError("a rejected assessment needs its reasons")
        if self.status == "error" and not self.error:
            raise ValueError("an error assessment needs the error")
        return self


class AuditReviewRequested(BaseModel):
    """The trigger of an audit review (phase 3 step 4b): quality.audit_trail_reviewer reads
    the ledger from from_seq through through_seq, always below this event's own seq."""

    model_config = _FROZEN

    type: Literal[EventType.AUDIT_REVIEW_REQUESTED] = EventType.AUDIT_REVIEW_REQUESTED
    study_id: StudyId
    from_seq: PositiveInt = 1
    through_seq: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.from_seq > self.through_seq:
            raise ValueError("from_seq may not exceed through_seq")
        return self


class QualityObservation(BaseModel):
    """A QA auditor's observation (phase 3 step 4b), attributed to the auditor. The same
    write appends a qa_observation finding that names this observation_id."""

    model_config = _FROZEN

    type: Literal[EventType.QUALITY_OBSERVATION] = EventType.QUALITY_OBSERVATION
    observation_id: Annotated[str, Field(pattern=r"^OB-[0-9a-f]{12}$")]
    raised_by: ActorId
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=4000)]
    severity: Severity
    refers_to_finding_id: FindingId | None = None
    refers_to_seq: PositiveInt | None = None


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
    | HumanDecision
    | AuditReviewRequested
    | QualityObservation,
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
