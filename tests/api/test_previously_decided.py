"""Decisions survive agent upgrades: a newer version's finding with the same identity as one
a person already accepted or dismissed, on unchanged primary evidence, carries that
decision and stays out of the queue. Derived at read time; no event is rewritten."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meridian.api.repository import primary_evidence_sha256
from meridian.domain.enums import Role
from meridian.domain.events import Event
from meridian.domain.findings import Evidence, Finding
from meridian.ledger.verify import verify
from tests.api_support import Fixture, blinded_finding, build, evidence, headers

OPS = headers(Role.CLINICAL_OPS_LEAD)
DEVIATIONS = "RBQM-0001"  # kri_monitor 2.2.0, site 1016, check deviations, 18 observed


def _get(client: TestClient, path: str) -> Any:
    r = client.get(path, headers=OPS)
    assert r.status_code == 200, r.text
    return r.json()


def _decide(client: TestClient, finding_id: str, action: str, reason: str | None = None) -> Any:
    body: dict[str, str] = {"action": action}
    if reason is not None:
        body["reason"] = reason
    r = client.post(f"/api/v1/findings/{finding_id}/decisions", json=body, headers=OPS)
    assert r.status_code == 201, r.text
    return r.json()


def _with_value(f: Finding, value: float) -> Finding:
    return f.model_copy(update={"evidence": (evidence().model_copy(update={"value": value}),)})


def _raise_again(fx: Fixture, version: str, value: float = 18.0) -> Event:
    """The deviations finding at 1016 on DC-BASE, raised by another kri_monitor version."""
    f = blinded_finding("1016", f"Site 1016: deviations ({version})", "again", version)
    return fx.raise_finding(_with_value(f, value))


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [i["finding_id"] for i in items]


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build(tmp_path, restricted=False)


def test_unchanged_evidence_carries_the_decision_and_leaves_the_queue(fx: Fixture) -> None:
    client = fx.client()
    decided = _decide(client, DEVIATIONS, "dismissed", "known site backlog, CAPA in place")
    _raise_again(fx, "2.3.0")  # RBQM-0003

    detail = _get(client, "/api/v1/findings/RBQM-0003")
    prev = detail["finding"]["previously_decided"]
    assert prev["finding_id"] == DEVIATIONS
    assert prev["agent_version"] == "2.2.0"
    assert (prev["action"], prev["actor"], prev["role"]) == (
        "dismissed",
        "asha.rao",
        "clinical_ops_lead",
    )
    assert prev["reason"] == "known site backlog, CAPA in place"
    assert prev["seq"] == decided["decision"]["seq"]
    assert prev["recorded_at"] == decided["decision"]["recorded_at"]
    assert detail["finding"]["evidence_changed_since"] is None
    assert detail["finding"]["status"] == "open"  # its own status: nobody decided it
    assert detail["decisions"] == []

    queue = _ids(_get(client, "/api/v1/review-queue")["items"])
    assert "RBQM-0003" not in queue
    assert queue == ["RBQM-0002"]
    assert "RBQM-0003" not in _ids(_get(client, "/api/v1/findings")["items"])
    widened = _get(client, "/api/v1/findings?include_previously_decided=true")["items"]
    assert "RBQM-0003" in _ids(widened)
    queue_widened = _get(client, "/api/v1/review-queue?include_previously_decided=true")
    assert "RBQM-0003" in _ids(queue_widened["items"])


def test_changed_evidence_enters_the_queue_and_shows_what_changed(fx: Fixture) -> None:
    client = fx.client()
    _decide(client, DEVIATIONS, "accepted")
    _raise_again(fx, "2.3.0", value=25.0)

    f = _get(client, "/api/v1/findings/RBQM-0003")["finding"]
    assert f["previously_decided"] is None
    changed = f["evidence_changed_since"]
    assert changed["decision"]["finding_id"] == DEVIATIONS
    assert changed["decision"]["action"] == "accepted"
    assert changed["before"] != changed["after"]
    assert changed["after"] == f["primary_evidence"]
    assert changed["removed"] == changed["before"]
    assert changed["added"] == changed["after"]
    assert changed["primary_evidence_sha256"] != changed["decision"]["primary_evidence_sha256"]
    assert "RBQM-0003" in _ids(_get(client, "/api/v1/review-queue")["items"])


def test_an_escalated_earlier_finding_is_still_open_and_carries_nothing(fx: Fixture) -> None:
    client = fx.client()
    _decide(client, DEVIATIONS, "escalated")
    _raise_again(fx, "2.3.0")

    f = _get(client, "/api/v1/findings/RBQM-0003")["finding"]
    assert (f["previously_decided"], f["evidence_changed_since"]) == (None, None)
    assert "RBQM-0003" in _ids(_get(client, "/api/v1/review-queue")["items"])


def test_escalated_then_dismissed_counts_as_closed(fx: Fixture) -> None:
    client = fx.client()
    _decide(client, DEVIATIONS, "escalated")
    _decide(client, DEVIATIONS, "dismissed", "resolved at the site visit")
    _raise_again(fx, "2.3.0")
    prev = _get(client, "/api/v1/findings/RBQM-0003")["finding"]["previously_decided"]
    assert prev["action"] == "dismissed"


def test_a_chain_across_three_versions_keeps_the_first_decision(fx: Fixture) -> None:
    client = fx.client()
    first = _decide(client, DEVIATIONS, "accepted")
    _raise_again(fx, "2.3.0")  # RBQM-0003
    _raise_again(fx, "2.4.0")  # RBQM-0004
    for finding_id in ("RBQM-0003", "RBQM-0004"):
        prev = _get(client, f"/api/v1/findings/{finding_id}")["finding"]["previously_decided"]
        assert (prev["finding_id"], prev["seq"]) == (DEVIATIONS, first["decision"]["seq"])
    queue = _ids(_get(client, "/api/v1/review-queue")["items"])
    assert not {"RBQM-0003", "RBQM-0004"} & set(queue)


def test_a_chain_uses_the_latest_decision_on_an_earlier_version(fx: Fixture) -> None:
    """2.2.0 accepted on 18; 2.3.0 sees 25 (changed, queued) and is dismissed; 2.4.0 sees 25
    again (unchanged since that dismissal), and 2.5.0 sees 18 (changed since it)."""
    client = fx.client()
    _decide(client, DEVIATIONS, "accepted")
    _raise_again(fx, "2.3.0", value=25.0)  # RBQM-0003
    assert "RBQM-0003" in _ids(_get(client, "/api/v1/review-queue")["items"])
    second = _decide(client, "RBQM-0003", "dismissed", "confirmed with the site")
    _raise_again(fx, "2.4.0", value=25.0)  # RBQM-0004
    prev = _get(client, "/api/v1/findings/RBQM-0004")["finding"]["previously_decided"]
    assert (prev["finding_id"], prev["seq"], prev["action"]) == (
        "RBQM-0003",
        second["decision"]["seq"],
        "dismissed",
    )
    # The newest decision governs: 18 matches 2.2.0's evidence, not 2.3.0's.
    _raise_again(fx, "2.5.0", value=18.0)  # RBQM-0005
    f = _get(client, "/api/v1/findings/RBQM-0005")["finding"]
    assert f["previously_decided"] is None
    assert f["evidence_changed_since"]["decision"]["finding_id"] == "RBQM-0003"
    assert _ids(_get(client, "/api/v1/review-queue")["items"])[0] == "RBQM-0005"


def test_a_finding_with_its_own_decision_is_governed_by_it(fx: Fixture) -> None:
    client = fx.client()
    _decide(client, DEVIATIONS, "dismissed", "known")
    _raise_again(fx, "2.3.0")
    _decide(client, "RBQM-0003", "escalated")
    f = _get(client, "/api/v1/findings/RBQM-0003")["finding"]
    assert (f["status"], f["previously_decided"]) == ("escalated", None)
    assert "RBQM-0003" in _ids(_get(client, "/api/v1/review-queue")["items"])


def test_another_check_or_site_does_not_inherit(fx: Fixture) -> None:
    client = fx.client()
    _decide(client, DEVIATIONS, "accepted")
    other_site = blinded_finding("1003", "Site 1003: deviations", "x", agent_version="2.3.0")
    fx.raise_finding(_with_value(other_site, 18.0))  # RBQM-0003
    other_check = blinded_finding("1016", "Site 1016: other", "x", agent_version="2.3.0")
    fx.raise_finding(_with_value(other_check, 18.0), check="query_rate")  # RBQM-0004
    for finding_id in ("RBQM-0003", "RBQM-0004"):
        f = _get(client, f"/api/v1/findings/{finding_id}")["finding"]
        assert (f["previously_decided"], f["evidence_changed_since"]) == (None, None)


def test_an_older_version_never_inherits_from_a_newer_one(fx: Fixture) -> None:
    client = fx.client()
    _raise_again(fx, "2.3.0")  # RBQM-0003
    _decide(client, "RBQM-0003", "accepted")
    f = _get(client, f"/api/v1/findings/{DEVIATIONS}")["finding"]
    assert f["previously_decided"] is None


def test_no_event_is_rewritten(fx: Fixture) -> None:
    client = fx.client()
    _decide(client, DEVIATIONS, "dismissed", "known")
    _raise_again(fx, "2.3.0")
    with fx.ledger() as ledger:
        before = [(e.seq, e.hash) for e in ledger.read()]
    _get(client, "/api/v1/review-queue")
    _get(client, "/api/v1/findings/RBQM-0003")
    with fx.ledger() as ledger:
        assert [(e.seq, e.hash) for e in ledger.read()] == before
        assert verify(ledger).ok


def test_the_evidence_hash_ignores_order_and_supporting_items() -> None:
    a = evidence()
    b = Evidence(
        source="ops/deviations",
        selector="SITEID=1016",
        statistic="deviations:expected",
        value=5.1,
    )
    f = _with_value(blinded_finding("1016", "t", "s"), 18.0)
    both = f.model_copy(update={"evidence": (a, b)})
    flipped = f.model_copy(update={"evidence": (b, a)})
    assert primary_evidence_sha256(both) == primary_evidence_sha256(flipped)
    changed = _with_value(f, 19.0)
    assert primary_evidence_sha256(changed) != primary_evidence_sha256(f)
