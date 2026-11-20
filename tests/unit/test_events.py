from datetime import datetime
from uuid import uuid7

import pytest
from pydantic import TypeAdapter, ValidationError

from meridian.domain.enums import DecisionAction, EventType, Role, Severity, Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    AssetOutlook,
    AttributionCorrection,
    AuditReviewRequested,
    ContainmentCheck,
    ContainmentCheckCompleted,
    CriterionResult,
    DataCut,
    DocumentFiled,
    Event,
    EventDraft,
    EventPayload,
    FindingAssessment,
    FindingNarrative,
    FindingRaised,
    GateDecision,
    GatePack,
    GateRequest,
    HumanDecision,
    InventoryLoaded,
    NoaelDecision,
    OpenItem,
    OutlookFact,
    PolicyDenied,
    PortfolioOutlook,
    PortfolioReviewRequested,
    QualityObservation,
    SendStudyLoaded,
    SignalScanRequested,
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
    SendStudyLoaded(study_id="MRD-101-TOX-01", cut_id="TOX-FINAL", path="data/send/x"),
    NoaelDecision(
        decision_id="NOAEL-0123456789ab",
        study_id="MRD-101-TOX-01",
        sex="combined",
        established=True,
        noael_dose=20.0,
        loael_dose=200.0,
        dose_unit="mg/kg/day",
        finding_ids=("PRE-0001",),
        rationale="Hepatocellular necrosis with ALT rise at 200 mg/kg; none at 20 mg/kg.",
        decided_by="leela.nair",
    ),
    InventoryLoaded(
        asset_id="MRD-101",
        cut_id="INV-0123abcd",
        path="data/inventory_cuts/MRD-101/INV-0123abcd",
        source_sha256="a" * 64,
    ),
    SignalScanRequested(
        study_id="MRD-201",
        scan_id="SCAN-20261120T000000Z",
        requested_by="operator:test",
        reuses_responses_of=7,
    ),
    AttributionCorrection(
        corrects_seq=7,
        field="requested_by",
        recorded_value="karan.mehta",
        true_value="operator:test",
        explanation="Started from the CLI by an operator, not by the medical monitor.",
        corrected_by="operator:test",
    ),
    PortfolioReviewRequested(
        review_id="REVIEW-20261120T000000Z", requested_by="rahul.iyer", reason="requested"
    ),
    GateRequest(
        gate_request_id="GATE-20261120T000000Z",
        asset_id="MRD-101",
        gate="ind_submission",
        requested_by="rahul.iyer",
    ),
    PortfolioOutlook(
        agent_id="portfolio.pipeline_intelligence",
        agent_version="1.0.0",
        trigger_seq=7,
        review_id="REVIEW-20261120T000000Z",
        dedup_key="k",
        summary="s",
        previous_outlook_seq=None,
        assets=(
            AssetOutlook(
                asset_id="MRD-110",
                status="static",
                static=True,
                status_text="On track",
                facts=(
                    OutlookFact(key="static:stage", group="static", label="Stage",
                                value="phase_1", status="static", seq=None, static=True),
                ),
                risks=(),
                changes=("First outlook: nothing to compare with.",),
            ),
        ),
    ),
    GatePack(
        agent_id="portfolio.stage_gate_pack",
        agent_version="1.0.0",
        trigger_seq=8,
        gate_request_id="GATE-20261120T000000Z",
        dedup_key="k2",
        asset_id="MRD-101",
        gate="ind_submission",
        gate_title="IND submission",
        summary="s",
        criteria=(
            CriterionResult(id="noael_decided", title="t", basis="policy", evidence="e",
                            status="met", explanation="x", seqs=(3,)),
        ),
        open_items=(OpenItem(criterion_id="noael_decided", text="t"),),
    ),
    GateDecision(
        gate_request_seq=8,
        pack_seq=9,
        asset_id="MRD-101",
        gate="ind_submission",
        decision="conditional_go",
        conditions=("Close the four gaps",),
        rationale="Go once the readiness gaps are closed.",
        decided_by="rahul.iyer",
    ),
]  # fmt: skip

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


NOAEL = {
    "decision_id": "NOAEL-0123456789ab",
    "study_id": "MRD-101-TOX-01",
    "sex": "M",
    "established": True,
    "noael_dose": 20.0,
    "dose_unit": "mg/kg/day",
    "rationale": "No adverse change at any dose up to 20 mg/kg/day.",
    "decided_by": "leela.nair",
}


@pytest.mark.parametrize(
    "changes",
    [
        {"established": False},  # a dose with "not established"
        {"noael_dose": None},  # established without a dose
        {"loael_dose": 20.0, "finding_ids": ("PRE-0001",)},  # LOAEL not above the NOAEL
        {"loael_dose": 200.0},  # a LOAEL resting on no finding
        {"finding_ids": ("PRE-0001", "PRE-0001")},
        {"supersedes": "NOAEL-0123456789ab"},  # itself
        {"rationale": "too short"},
        {"sex": "both"},
    ],
)
def test_noael_decision_rules(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NoaelDecision.model_validate({**NOAEL, **changes})


def test_a_noael_not_established_has_no_dose() -> None:
    d = NoaelDecision.model_validate(
        {**NOAEL, "established": False, "noael_dose": None, "loael_dose": 2.0,
         "finding_ids": ("PRE-0002",)}
    )  # fmt: skip
    assert d.noael_dose is None
    assert d.loael_dose == 2.0
