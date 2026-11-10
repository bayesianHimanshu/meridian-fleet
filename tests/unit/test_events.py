from datetime import datetime
from uuid import uuid7

import pytest
from pydantic import TypeAdapter, ValidationError

from meridian.domain.enums import DecisionAction, EventType, Role, Severity, Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    AuditReviewRequested,
    ContainmentCheck,
    ContainmentCheckCompleted,
    DataCut,
    DocumentFiled,
    Event,
    EventDraft,
    EventPayload,
    FindingAssessment,
    FindingNarrative,
    FindingRaised,
    HumanDecision,
    PolicyDenied,
    QualityObservation,
)
from meridian.domain.findings import Decision
from tests.support import STUDY, T0, make_finding

PAYLOADS: list[EventPayload] = [
    DataCut(cut_id="CUT-1", study_id=STUDY, path="cuts/1.parquet"),
    DocumentFiled(doc_id="DOC-9", study_id=STUDY, zone="05.02", artifact="protocol_v3.pdf"),
    ActorWoke(agent_id="rbqm.kri_sentinel", trigger_seq=1),
    ActorSuspended(agent_id="rbqm.kri_sentinel", trigger_seq=2),
    FindingRaised(finding=make_finding(), dedup_key="rbqm:kri:SITE-001:cut-1"),
    PolicyDenied(
        agent_id="safety.ae_watch",
        resource="unblinded/arms.parquet",
        policy="blinding",
        reason="agent is blinded",
    ),
    PolicyDenied(
        agent_id="system.conformance",
        resource="192.0.2.1:443",
        policy="policies/rbqm.yaml",
        reason="transparent_tcp_policy_denied",
        source="openshell_log",
        zone="rbqm",
        check="egress_test_net",
        sandbox="mer-cf-rb-1a2b3c4d",
        policy_sha256="0" * 64,
        raw_line=(
            "[1794055103.727] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED "
            "/usr/bin/bash(0) -> 192.0.2.1:443 [reason:transparent_tcp_policy_denied]"
        ),
    ),
    ContainmentCheckCompleted(
        zone="rbqm",
        sandbox="mer-cf-rb-1a2b3c4d",
        image="meridian-agent:0.1.0",
        image_digest="sha256:" + "1" * 64,
        policy_sha256="2" * 64,
        checks=(
            ContainmentCheck(
                name="read_outside_allow_list",
                expect="refused",
                command="cat /etc/os-release",
                outcome="refused",
                exit_code=1,
                message="cat: /etc/os-release: Permission denied",
                passed=True,
            ),
        ),
        log_line_found=True,
        all_refused=True,
        passed=True,
    ),
    ContainmentCheckCompleted(
        zone="rbqm",
        sandbox="mer-cf-rn-1a2b3c4d",
        image="meridian-agent:0.1.0",
        image_digest="sha256:" + "1" * 64,
        policy_sha256="2" * 64,
        checks=(),
        log_line_found=True,
        all_refused=True,
        passed=True,
        policy_name="rbqm.narrator",
        providers=("meridian-anthropic",),
        credential_value_visible=False,
    ),
    FindingNarrative(
        finding_id="RBQM-0027",
        finding_seq=40,
        agent_id="rbqm.narrator",
        agent_version="1.0.0",
        status="accepted",
        headline="Site 1005 reports fewer adverse events than expected",
        explanation="Site 1005 has 27 adverse events where 70.2 were expected.",
        model="claude-haiku-5-5",
        request_sha256="3" * 64,
        response_sha256="4" * 64,
        input_tokens=812,
        output_tokens=61,
        latency_ms=1450,
        executor="openshell",
        sandbox="mer-nr-1a2b3c4d",
        policy_sha256="5" * 64,
    ),
    FindingNarrative(
        finding_id="RBQM-0028",
        finding_seq=41,
        agent_id="rbqm.narrator",
        agent_version="1.0.0",
        status="refused",
        model="claude-haiku-5-5",
        rejection_reasons=("subject_id_pattern",),
    ),
    FindingAssessment(
        finding_ids=("SAF-0012", "SAF-0013"),
        finding_seqs=(52, 53),
        candidate="unpaired_pair",
        agent_id="safety.term_adjudicator",
        agent_version="1.0.0",
        status="accepted",
        verdict="unsure",
        reason="The terms may name the same allergic reaction.",
        model="granite4:3b",
        model_digest="6" * 64,
        request_sha256="7" * 64,
        response_sha256="8" * 64,
        input_tokens=140,
        output_tokens=24,
        latency_ms=310,
        executor="openshell",
        sandbox="mer-lm-1a2b3c4d",
        policy_sha256="9" * 64,
    ),
    HumanDecision(
        decision=Decision(
            finding_id="RBQM-0001",
            action=DecisionAction.ESCALATED,
            actor="u.lee",
            role=Role.CLINICAL_OPS_LEAD,
        )
    ),
    AuditReviewRequested(study_id="MRD-201", through_seq=41),
    QualityObservation(
        observation_id="OB-0123456789ab",
        raised_by="nina.fernandes",
        title="Dismissal reason too thin",
        description="The dismissal of RBQM-0001 gives no reason a reviewer can check.",
        severity=Severity.MEDIUM,
        refers_to_finding_id="RBQM-0001",
    ),
]

ADAPTER: TypeAdapter[EventPayload] = TypeAdapter(EventPayload)


def test_samples_cover_every_event_type() -> None:
    assert {p.type for p in PAYLOADS} == set(EventType)


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: str(p.type))
def test_payload_round_trips_through_json(payload: EventPayload) -> None:
    restored = ADAPTER.validate_json(ADAPTER.dump_json(payload))
    assert restored == payload
    assert type(restored) is type(payload)


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: str(p.type))
def test_event_envelope_round_trips_through_json(payload: EventPayload) -> None:
    event = Event(
        seq=1,
        event_id=uuid7(),
        occurred_at=T0,
        recorded_at=T0,
        actor="tester",
        study_id=STUDY,
        visibility=Visibility.BLINDED,
        payload=payload,
        prev_hash="0" * 64,
        hash="a" * 64,
    )
    assert Event.model_validate_json(event.model_dump_json()) == event
    assert event.event_type is payload.type


def test_unknown_payload_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"type": "nope"})


def test_draft_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        EventDraft(
            occurred_at=datetime(2026, 1, 1),  # noqa: DTZ001
            actor="tester",
            visibility=Visibility.BLINDED,
            payload=PAYLOADS[0],
        )


def test_draft_normalizes_to_utc() -> None:
    draft = EventDraft(
        occurred_at=datetime.fromisoformat("2026-01-01T12:00:00+05:30"),
        actor="tester",
        visibility=Visibility.BLINDED,
        payload=PAYLOADS[0],
    )
    assert draft.occurred_at.isoformat() == "2026-01-01T06:30:00+00:00"


def test_restricted_finding_requires_restricted_event() -> None:
    finding = make_finding(
        agent_id="unblinded.efficacy_watch",
        visibility=Visibility.RESTRICTED,
        visible_to=frozenset({Role.UNBLINDED_STATISTICIAN}),
    )
    with pytest.raises(ValidationError, match="restricted"):
        EventDraft(
            occurred_at=T0,
            actor="unblinded.efficacy_watch",
            visibility=Visibility.BLINDED,
            payload=FindingRaised(finding=finding, dedup_key="k"),
        )


def _narrative(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "finding_id": "RBQM-0001",
        "finding_seq": 3,
        "agent_id": "rbqm.narrator",
        "agent_version": "1.0.0",
        "model": "m",
        "request_sha256": "a" * 64,
    }
    return base | changes


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "accepted"},  # no text
        {"status": "rejected", "headline": "h", "rejection_reasons": ("em_dash",)},
        {"status": "rejected"},  # no reasons
        {"status": "error"},  # no error text
        {"status": "refused", "rejection_reasons": ("x",)},  # refused but a request hash
        {"status": "accepted", "headline": "h", "explanation": "e", "request_sha256": None},
    ],
)
def test_finding_narrative_rules(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FindingNarrative.model_validate(_narrative(**changes))


def test_a_review_range_lies_before_its_trigger() -> None:
    import pytest
    from pydantic import ValidationError

    ok = ActorWoke(
        agent_id="quality.audit_trail_reviewer", trigger_seq=42, review_from_seq=1, review_to_seq=41
    )
    assert (ok.review_from_seq, ok.review_to_seq) == (1, 41)
    for bad in (
        {"review_from_seq": 1},
        {"review_from_seq": 5, "review_to_seq": 4},
        {"review_from_seq": 1, "review_to_seq": 42},
    ):
        with pytest.raises(ValidationError):
            ActorWoke(agent_id="quality.audit_trail_reviewer", trigger_seq=42, **bad)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AuditReviewRequested(study_id="MRD-201", from_seq=5, through_seq=4)
