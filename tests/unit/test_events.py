from datetime import datetime
from uuid import uuid7

import pytest
from pydantic import TypeAdapter, ValidationError

from meridian.domain.enums import DecisionAction, EventType, Role, Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    ContainmentCheck,
    ContainmentCheckCompleted,
    DataCut,
    DocumentFiled,
    Event,
    EventDraft,
    EventPayload,
    FindingRaised,
    HumanDecision,
    PolicyDenied,
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
    HumanDecision(
        decision=Decision(
            finding_id="RBQM-0001",
            action=DecisionAction.ESCALATED,
            actor="u.lee",
            role=Role.CLINICAL_OPS_LEAD,
        )
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
