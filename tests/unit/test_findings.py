import pytest
from pydantic import ValidationError

from meridian.domain.enums import DecisionAction, Role
from meridian.domain.findings import Decision, Finding
from tests.support import make_finding


def test_dismiss_without_reason_fails() -> None:
    with pytest.raises(ValidationError, match="reason is required"):
        Decision(
            finding_id="RBQM-0001",
            action=DecisionAction.DISMISSED,
            actor="u.lee",
            role=Role.CLINICAL_OPS_LEAD,
        )


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_dismiss_with_blank_reason_fails(blank: str) -> None:
    with pytest.raises(ValidationError, match="reason is required"):
        Decision(
            finding_id="RBQM-0001",
            action=DecisionAction.DISMISSED,
            actor="u.lee",
            role=Role.CLINICAL_OPS_LEAD,
            reason=blank,
        )


def test_dismiss_with_reason_succeeds() -> None:
    decision = Decision(
        finding_id="RBQM-0001",
        action=DecisionAction.DISMISSED,
        actor="u.lee",
        role=Role.CLINICAL_OPS_LEAD,
        reason="Site already on a corrective action plan.",
    )
    assert decision.action is DecisionAction.DISMISSED


@pytest.mark.parametrize("action", list(DecisionAction))
def test_qa_auditor_decision_fails(action: DecisionAction) -> None:
    with pytest.raises(ValidationError, match="qa_auditor"):
        Decision(
            finding_id="QA-0001",
            action=action,
            actor="u.audit",
            role=Role.QA_AUDITOR,
            reason="looked at it",
        )


def test_accept_without_reason_succeeds() -> None:
    decision = Decision(
        finding_id="SAF-0002",
        action=DecisionAction.ACCEPTED,
        actor="u.mm",
        role=Role.MEDICAL_MONITOR,
    )
    assert decision.reason is None


def test_finding_requires_at_least_one_evidence() -> None:
    data = make_finding().model_dump()
    data["evidence"] = ()
    with pytest.raises(ValidationError, match="at least 1 item"):
        Finding.model_validate(data)


def test_finding_is_frozen() -> None:
    finding = make_finding()
    with pytest.raises(ValidationError):
        setattr(finding, "score", 0.1)  # noqa: B010


def test_finding_rejects_non_finite_score() -> None:
    data = make_finding().model_dump()
    data["score"] = float("nan")
    with pytest.raises(ValidationError):
        Finding.model_validate(data)
