"""POST /findings/{id}/decisions: each rule, one event per decision, and concurrency."""

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meridian.domain.enums import DecisionAction, EventType, Role
from meridian.domain.events import Event, EventDraft, HumanDecision
from meridian.domain.findings import Decision
from meridian.ledger.verify import verify
from tests.api_support import T0, Fixture, build, headers

URL = "/api/v1/findings/{}/decisions"


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build(tmp_path)


def _decisions(fx: Fixture) -> list[Event]:
    with fx.ledger() as ledger:
        return [
            e
            for e in ledger.read(
                event_types=[EventType.HUMAN_DECISION], viewer_role=Role.UNBLINDED_STATISTICIAN
            )
            if isinstance(e, Event)
        ]


def test_an_accept_appends_exactly_one_human_decision(fx: Fixture) -> None:
    client = fx.client()
    with fx.ledger() as ledger:
        before = len(ledger.read())
    r = client.post(
        URL.format("RBQM-0001"),
        json={"action": "accepted"},
        headers=headers(Role.CLINICAL_OPS_LEAD),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "accepted"
    (event,) = _decisions(fx)
    assert body["decision"]["seq"] == event.seq == before + 1
    assert body["decision"]["hash"] == event.hash
    assert body["decision"]["prev_hash"] == event.prev_hash
    assert isinstance(event.payload, HumanDecision)
    d = event.payload.decision
    assert (d.actor, d.role, d.action) == (
        "asha.rao",
        Role.CLINICAL_OPS_LEAD,
        DecisionAction.ACCEPTED,
    )
    assert event.actor == "asha.rao"
    with fx.ledger() as ledger:
        assert len(ledger.read()) == before + 1
        assert verify(ledger).ok
    detail = client.get("/api/v1/findings/RBQM-0001", headers=headers(Role.MEDICAL_MONITOR)).json()
    assert detail["finding"]["status"] == "accepted"
    assert detail["finding"]["decision_count"] == 1
    assert detail["decisions"][0]["actor"] == "asha.rao"


def test_qa_auditor_may_never_decide(fx: Fixture) -> None:
    r = fx.client().post(
        URL.format("RBQM-0001"), json={"action": "accepted"}, headers=headers(Role.QA_AUDITOR)
    )
    assert r.status_code == 403
    assert "qa_auditor" in r.json()["detail"]
    assert _decisions(fx) == []


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_a_dismissal_needs_a_reason(fx: Fixture, reason: str | None) -> None:
    body: dict[str, object] = {"action": "dismissed"}
    if reason is not None:
        body["reason"] = reason
    r = fx.client().post(
        URL.format("RBQM-0001"), json=body, headers=headers(Role.CLINICAL_OPS_LEAD)
    )
    assert r.status_code == 422
    assert _decisions(fx) == []


def test_a_bad_action_is_422(fx: Fixture) -> None:
    r = fx.client().post(
        URL.format("RBQM-0001"), json={"action": "deleted"}, headers=headers(Role.CLINICAL_OPS_LEAD)
    )
    assert r.status_code == 422


@pytest.mark.parametrize("first", ["accepted", "dismissed"])
def test_a_closed_finding_is_409(fx: Fixture, first: str) -> None:
    client = fx.client()
    h = headers(Role.CLINICAL_OPS_LEAD)
    assert (
        client.post(
            URL.format("RBQM-0001"), json={"action": first, "reason": "dup"}, headers=h
        ).status_code
        == 201
    )
    again = client.post(
        URL.format("RBQM-0001"), json={"action": "escalated"}, headers=headers(Role.MEDICAL_MONITOR)
    )
    assert again.status_code == 409
    assert first in again.json()["detail"]
    assert len(_decisions(fx)) == 1


def test_an_escalation_may_be_followed_by_another_decision(fx: Fixture) -> None:
    client = fx.client()
    assert (
        client.post(
            URL.format("RBQM-0001"),
            json={"action": "escalated"},
            headers=headers(Role.CLINICAL_OPS_LEAD),
        ).status_code
        == 201
    )
    r = client.post(
        URL.format("RBQM-0001"),
        json={"action": "dismissed", "reason": "duplicate of a known issue"},
        headers=headers(Role.MEDICAL_MONITOR),
    )
    assert r.status_code == 201
    assert [e.payload.decision.action for e in _decisions(fx)] == [  # type: ignore[union-attr]
        DecisionAction.ESCALATED,
        DecisionAction.DISMISSED,
    ]


@pytest.mark.parametrize(
    ("role", "finding"),
    [
        (Role.PORTFOLIO_LEAD, "RBQM-0001"),  # not in visible_to
        (Role.CLINICAL_OPS_LEAD, "DSMB-0001"),  # restricted
        (Role.QA_AUDITOR, "DSMB-0001"),  # not visible beats qa_auditor's 403
        (Role.CLINICAL_OPS_LEAD, "RBQM-9999"),  # does not exist
    ],
)
def test_a_finding_the_caller_may_not_see_is_404_not_403(
    fx: Fixture, role: Role, finding: str
) -> None:
    r = fx.client().post(URL.format(finding), json={"action": "accepted"}, headers=headers(role))
    assert r.status_code == 404
    assert r.json() == {"detail": "finding not found"}  # the same text either way
    assert _decisions(fx) == []


def test_a_decision_on_a_restricted_finding_is_restricted(fx: Fixture) -> None:
    r = fx.client().post(
        URL.format("DSMB-0001"),
        json={"action": "escalated"},
        headers=headers(Role.UNBLINDED_STATISTICIAN),
    )
    assert r.status_code == 201
    (event,) = _decisions(fx)
    assert event.visibility.value == "restricted"
    with fx.ledger() as ledger:
        (shown,) = ledger.read(since_seq=event.seq - 1, viewer_role=Role.CLINICAL_OPS_LEAD)
    assert not isinstance(shown, Event)  # redacted for a blinded role


def test_concurrent_decisions_give_one_success_and_one_409(fx: Fixture) -> None:
    app = fx.client().app
    barrier = threading.Barrier(2)
    statuses: list[int] = []
    lock = threading.Lock()

    def post(role: Role, action: str) -> None:
        with TestClient(app) as client:
            barrier.wait()
            r = client.post(
                URL.format("RBQM-0001"),
                json={"action": action, "reason": "x"},
                headers=headers(role),
            )
            with lock:
                statuses.append(r.status_code)

    threads = [
        threading.Thread(target=post, args=(Role.CLINICAL_OPS_LEAD, "accepted")),
        threading.Thread(target=post, args=(Role.MEDICAL_MONITOR, "dismissed")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [201, 409]
    assert len(_decisions(fx)) == 1
    with fx.ledger() as ledger:
        assert verify(ledger).ok


def test_the_guard_runs_under_the_write_lock(fx: Fixture) -> None:
    """While one guarded append is between its check and its write, another waits; when it
    gets the lock its own check sees the first decision. Two ledger handles, one file."""
    inside, results = threading.Event(), {}

    def draft(action: DecisionAction) -> EventDraft:
        return EventDraft(
            occurred_at=T0,
            actor="asha.rao",
            visibility=fx.events[0].visibility,
            payload=HumanDecision(
                decision=Decision(
                    finding_id="RBQM-0001",
                    action=action,
                    actor="asha.rao",
                    role=Role.CLINICAL_OPS_LEAD,
                    reason="r",
                )
            ),
        )

    def no_decision_yet(ledger: object) -> None:
        assert hasattr(ledger, "read")
        if ledger.read(event_types=[EventType.HUMAN_DECISION]):
            raise RuntimeError("already decided")

    def first() -> None:
        with fx.ledger() as ledger:

            def slow_guard() -> None:
                no_decision_yet(ledger)
                inside.set()
                time.sleep(0.3)  # the second writer arrives now

            ledger.append_guarded(draft(DecisionAction.ACCEPTED), slow_guard)
            results["first"] = "written"

    def second() -> None:
        inside.wait()
        with fx.ledger() as ledger:
            try:
                ledger.append_guarded(
                    draft(DecisionAction.DISMISSED), lambda: no_decision_yet(ledger)
                )
                results["second"] = "written"
            except RuntimeError as exc:
                results["second"] = str(exc)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {"first": "written", "second": "already decided"}
    assert len(_decisions(fx)) == 1
