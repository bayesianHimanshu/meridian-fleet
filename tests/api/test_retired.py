"""Retired findings: a newer version ran on the cut and raised nothing with the same key."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meridian.domain.enums import Role
from meridian.domain.events import ActorSuspended, ActorWoke
from meridian.platform.config import Settings
from tests.api_support import STUDY, Fixture, blinded_finding, build, headers

OPS = headers(Role.CLINICAL_OPS_LEAD)


def _run(fx: Fixture, agent: str, version: str, trigger: int = 1) -> None:
    fx.append(
        ActorWoke(agent_id=agent, agent_version=version, trigger_seq=trigger, executor="openshell")
    )
    fx.append(
        ActorSuspended(
            agent_id=agent, agent_version=version, trigger_seq=trigger, executor="openshell"
        )
    )


def _get(client: TestClient, path: str) -> Any:
    r = client.get(path, headers=OPS)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    """RBQM-0001 (1016, deviations) and RBQM-0002 (1004, lag) are kri_monitor 2.2.0 on
    DC-BASE. A 2.3.0 run on DC-BASE then raises only 1016's deviations again."""
    fx = build(tmp_path, restricted=False)
    fx.raise_finding(
        blinded_finding("1016", "Site 1016: deviations (2.3.0)", "new", agent_version="2.3.0")
    )
    _run(fx, "rbqm.kri_monitor", "2.3.0")
    return fx


def test_a_finding_the_newer_run_did_not_raise_is_retired(fx: Fixture) -> None:
    every = _get(fx.client(), "/api/v1/findings?include_superseded=true&include_retired=true")
    by_id = {i["finding_id"]: i for i in every["items"]}
    lag = by_id["RBQM-0002"]
    assert (lag["status"], lag["retired"], lag["retired_by_version"]) == ("retired", True, "2.3.0")
    dev = by_id["RBQM-0001"]  # raised again by 2.3.0: superseded, not retired
    assert (dev["superseded"], dev["superseded_by"], dev["retired"]) == (True, "RBQM-0003", False)
    assert by_id["RBQM-0003"]["status"] == "open"
    assert by_id["RBQM-0003"]["retired"] is False


def test_retired_findings_leave_the_queue_and_the_default_list(fx: Fixture) -> None:
    client = fx.client()
    assert [i["finding_id"] for i in _get(client, "/api/v1/findings")["items"]] == ["RBQM-0003"]
    assert [i["finding_id"] for i in _get(client, "/api/v1/review-queue")["items"]] == ["RBQM-0003"]
    widened = _get(client, "/api/v1/review-queue?include_retired=true")["items"]
    assert "RBQM-0002" not in [i["finding_id"] for i in widened]  # not open: retired
    listed = _get(client, "/api/v1/findings?include_retired=true")["items"]
    assert [i["finding_id"] for i in listed] == ["RBQM-0003", "RBQM-0002"]
    assert _get(client, "/api/v1/findings?include_retired=true&status=retired")["total"] == 1
    # It stays readable in full.
    assert _get(client, "/api/v1/findings/RBQM-0002")["finding"]["status"] == "retired"


def test_a_run_that_did_not_complete_retires_nothing(tmp_path: Path) -> None:
    fx = build(tmp_path, restricted=False)
    fx.append(
        ActorWoke(
            agent_id="rbqm.kri_monitor", agent_version="2.3.0", trigger_seq=1, executor="openshell"
        )
    )
    items = _get(fx.client(), "/api/v1/findings")["items"]
    assert [i["status"] for i in items] == ["open", "open"]


def test_a_newer_run_on_another_cut_retires_nothing(tmp_path: Path) -> None:
    fx = build(tmp_path, restricted=False)
    _run(fx, "rbqm.kri_monitor", "2.3.0", trigger=2)  # the opaque cut, not DC-BASE
    assert [i["retired"] for i in _get(fx.client(), "/api/v1/findings")["items"]] == [False, False]


def test_a_closed_finding_keeps_its_decision_but_is_marked_retired(tmp_path: Path) -> None:
    fx = build(tmp_path, restricted=False)
    client = fx.client()
    assert (
        client.post(
            "/api/v1/findings/RBQM-0002/decisions", json={"action": "accepted"}, headers=OPS
        ).status_code
        == 201
    )
    _run(fx, "rbqm.kri_monitor", "2.3.0")
    lag = _get(client, "/api/v1/findings/RBQM-0002")["finding"]
    assert (lag["status"], lag["retired"]) == ("accepted", True)


def test_outlines_come_from_current_findings_only(fx: Fixture, tmp_path: Path) -> None:
    from tests.agent_support import clean
    from tests.rbqm_support import write_tables_cut

    blinded, ops, unblinded = clean()
    write_tables_cut(tmp_path / "curated", "DC-BASE", blinded, ops, unblinded)
    # A current 2.3.0 finding at 1003, a site the fixture cut has. 1004's 2.2.0 finding is
    # retired (2.3.0 ran on DC-BASE and raised nothing there), so it gets no outline.
    fx.raise_finding(
        blinded_finding("1003", "Site 1003: deviations", "x", agent_version="2.3.0"),
        check="deviations",
    )
    client = fx.client(curated_root=tmp_path / "curated")
    sites = _get(client, "/api/v1/rbqm/DC-BASE/sites")["sites"]
    flags = {
        s["site_id"]: [(f["check"], f["finding_id"], f["agent_version"]) for f in s["flagged_by"]]
        for s in sites
    }
    assert flags["1003"] == [("deviations", "RBQM-0004", "2.3.0")]
    assert flags["1004"] == []
    assert all(not v for k, v in flags.items() if k != "1003")


def test_on_dc_base_outlines_match_the_current_site_findings() -> None:
    """On the local DC-BASE (skipped without the real ledger and data): every outline is a
    current site-level finding, and every current site-level finding has an outline."""
    from meridian.cli import _api
    from meridian.platform.clock import SystemClock

    s = Settings()
    if not s.ledger_path.exists() or not (s.curated_root / s.study_id / "DC-BASE").exists():
        pytest.skip("no local ledger or DC-BASE data")
    client = TestClient(_api(s, SystemClock()))
    current = {
        (i["site_id"], i["check"], i["finding_id"])
        for i in _get(client, "/api/v1/findings?cut=DC-BASE&limit=500")["items"]
        if i["site_id"] is not None
    }
    sites = _get(client, "/api/v1/rbqm/DC-BASE/sites")["sites"]
    outlined = {
        (s_["site_id"], f["check"], f["finding_id"]) for s_ in sites for f in s_["flagged_by"]
    }
    assert outlined == current, json.dumps(sorted(outlined ^ current))
    assert s.study_id == STUDY
