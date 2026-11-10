"""Fleet page: a run that started and has not finished within the run timeout shows as
running, not failed; after the timeout it is a failed run."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from meridian.api.app import create_app
from meridian.domain.enums import Role
from meridian.domain.events import ActorWoke
from meridian.platform.clock import FixedClock
from tests.api_support import T0, Fixture, build, headers

AGENT = "quality.tmf_completeness"


def _agent(fx: Fixture, after: timedelta, timeout: int = 120) -> dict[str, Any]:
    context = replace(fx.context(), clock=FixedClock(T0 + after), run_timeout_seconds=timeout)
    body = TestClient(create_app(context)).get("/api/v1/fleet", headers=headers(Role.QA_AUDITOR))
    agents: dict[str, dict[str, Any]] = {a["agent_id"]: a for a in body.json()["agents"]}
    return agents[AGENT]


def test_a_started_run_is_running_until_its_timeout_then_failed(tmp_path: Path) -> None:
    fx = build(tmp_path, restricted=False)
    fx.append(ActorWoke(agent_id=AGENT, agent_version="1.0.0", trigger_seq=1), actor=AGENT)
    soon = _agent(fx, timedelta(seconds=30))
    assert (soon["last_outcome"], soon["failed_7d"], soon["runs_7d"]) == ("running", 0, 1)
    assert soon["last_failure_at"] is None
    at_limit = _agent(fx, timedelta(seconds=120))
    assert at_limit["last_outcome"] == "running"
    late = _agent(fx, timedelta(seconds=121))
    assert (late["last_outcome"], late["failed_7d"]) == ("not_completed", 1)
    assert late["last_failure_at"] is not None
    # The timeout is the configured one.
    assert _agent(fx, timedelta(seconds=121), timeout=600)["last_outcome"] == "running"


def test_the_run_timeout_comes_from_the_runtime_settings() -> None:
    from meridian.api.app import context_from_settings
    from meridian.platform.config import Settings

    s = Settings()
    repo_root = Path(__file__).resolve().parents[2]
    ctx = context_from_settings(
        s,
        FixedClock(T0),
        repo_root / "config" / "people.toml",
        repo_root / "config" / "portfolio.toml",
    )
    assert ctx.run_timeout_seconds == s.runtime.exec_timeout_seconds
