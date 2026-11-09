"""The fleet page's run counts and policy hash pair, and the ledger's agent filter.

A detector run failed when its ActorWoke has no ActorSuspended for the same agent, version
and trigger before that key's next ActorWoke. Failed events stay in the ledger as they are.
"""

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meridian.domain.enums import Role, Visibility
from meridian.domain.events import ActorSuspended, ActorWoke, EventDraft, EventPayload
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import FixedClock
from meridian.platform.config import DEFAULT_FINDING_ID_PREFIXES
from meridian.sandbox import policies
from tests.api_support import REPO_ROOT, STUDY, T0, Fixture, build, headers

OPS = headers(Role.CLINICAL_OPS_LEAD)
KRI = "rbqm.kri_monitor"


def _get(client: TestClient, path: str, h: dict[str, str] = OPS) -> Any:
    response = client.get(path, headers=h)
    assert response.status_code == 200, response.text
    return response.json()


def _at(fx: Fixture, when: timedelta, payload: EventPayload) -> int:
    """Append as of T0 + when (the fixture's own events are all at T0)."""
    with SqliteLedger(
        path=fx.ledger_path,
        clock=FixedClock(T0 + when),
        finding_id_prefixes=DEFAULT_FINDING_ID_PREFIXES,
    ) as ledger:
        event = ledger.append(
            EventDraft(
                occurred_at=T0 + when,
                actor=KRI,
                study_id=STUDY,
                visibility=Visibility.BLINDED,
                payload=payload,
            )
        )
    fx.events.append(event)
    return event.seq


def _woke(trigger: int, version: str = "2.2.0") -> ActorWoke:
    return ActorWoke(agent_id=KRI, agent_version=version, trigger_seq=trigger, executor="openshell")


def _done(trigger: int, version: str = "2.2.0") -> ActorSuspended:
    return ActorSuspended(
        agent_id=KRI, agent_version=version, trigger_seq=trigger, executor="openshell"
    )


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    fx = build(tmp_path / "api")
    # The fixture already holds one completed run of 2.2.0 on trigger 1, at T0. The clock
    # the API reads is T0, so "the last 7 days" is T0 - 7 days to T0.
    _at(fx, -timedelta(days=9), _woke(2, "2.1.0"))  # failed, before the window
    _at(fx, -timedelta(days=2), _woke(2))  # failed: its retry below has its own woke
    _at(fx, -timedelta(days=2, hours=-1), _woke(2))
    _at(fx, -timedelta(days=2, hours=-2), _done(2))
    _at(fx, -timedelta(hours=3), _woke(1))  # failed, the latest failure
    return fx


def test_the_fleet_counts_failed_runs_from_the_ledger(fx: Fixture) -> None:
    kri = {a["agent_id"]: a for a in _get(fx.client(), "/api/v1/fleet")["agents"]}[KRI]
    # In the window: the fixture's run, the failed attempt, its retry, the last failure.
    assert (kri["runs_7d"], kri["failed_7d"]) == (4, 2)
    assert kri["last_failure_at"].startswith((T0 - timedelta(hours=3)).strftime("%Y-%m-%dT%H"))
    # The latest run of the current version is the one that never finished.
    assert kri["last_outcome"] == "not_completed"
    assert kri["run_event_types"] == ["actor_woke", "actor_suspended"]


def test_the_ledger_filters_by_agent_and_keeps_the_failed_events(fx: Fixture) -> None:
    client = fx.client()
    page = _get(
        client,
        f"/api/v1/ledger?agent={KRI}&type=actor_woke&type=actor_suspended&order=desc&limit=100",
    )
    types = [(e["payload"]["type"], e["payload"]["trigger_seq"]) for e in page["entries"]]
    assert types == [
        ("actor_woke", 1),
        ("actor_suspended", 2),
        ("actor_woke", 2),
        ("actor_woke", 2),
        ("actor_woke", 2),
        ("actor_suspended", 1),
        ("actor_woke", 1),
    ]
    findings = _get(client, f"/api/v1/ledger?agent={KRI}&type=finding_raised")["entries"]
    assert len(findings) == 2
    # A restricted event is never matched for a role that sees it redacted.
    restricted = "/api/v1/ledger?agent=unblinded.efficacy_watch&type=finding_raised"
    assert _get(client, restricted)["entries"] == []
    stat = headers(Role.UNBLINDED_STATISTICIAN)
    assert len(_get(client, restricted, stat)["entries"]) == 1


def test_fleet_and_runs_show_the_same_policy_pair(fx: Fixture) -> None:
    by_id = {a["agent_id"]: a for a in _get(fx.client(), "/api/v1/fleet")["agents"]}
    for agent_id, name in (
        ("rbqm.qtl_detector", "rbqm"),
        ("safety.sae_reconciler", "safety.sae_reconciler"),
        ("rbqm.narrator", "rbqm.narrator"),
    ):
        a = by_id[agent_id]
        path = REPO_ROOT / "policies" / f"{name}.yaml"
        assert a["policy"] == name
        assert a["policy_file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert a["applied_policy_sha256"] == policies.sha256_text(
            policies.expected_effective(policies.ALL_POLICIES[name])
        )
    rbqm, safety = by_id["rbqm.qtl_detector"], by_id["safety.sae_reconciler"]
    # The safety zone reads no operational data, so its rules differ from rbqm's.
    assert rbqm["applied_policy_sha256"] != safety["applied_policy_sha256"]
    # Two files that differ only in their header comment apply the same policy.
    assert safety["policy_file_sha256"] != policies.file_sha256("safety", REPO_ROOT / "policies")
    assert safety["applied_policy_sha256"] == policies.applied_sha256("safety")
    assert by_id["rbqm.narrator"]["applied_policy_sha256"] != rbqm["applied_policy_sha256"]
