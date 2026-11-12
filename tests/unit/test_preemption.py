"""A run whose worker is lost (a Spot preemption) fails and is retried once on a new
actor (phase 4b step 2). Simulated: the executor raises WorkerLostError on chosen runs."""

from pathlib import Path
from typing import Any

import structlog

from meridian.agents.base import AgentConfig
from meridian.agents.registry import AgentRegistry
from meridian.agents.runner import AgentRunner, RunPlan
from meridian.data.datacut import record_data_cut
from meridian.domain.events import ActorSuspended, ActorWoke, Event, FindingRaised, SubstrateRun
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.sandbox.executor import InProcessExecutor
from meridian.sandbox.substrate_executor import WorkerLostError
from tests.support import T0, make_ledger
from tests.unit.test_runner import STUDY, FakeAgent, StubReader

AGENTS = ("rbqm.alpha", "rbqm.bravo")
CUTS = ("DC-A", "DC-B")


def _placement(actor: str) -> SubstrateRun:
    return SubstrateRun(
        version="v1.0.0",
        commit="b" * 40,
        atespace="meridian",
        template="rbqm-cccc",
        template_sha256="7" * 64,
        template_file="deploy/substrate/templates/rbqm.yaml",
        template_file_sha256="6" * 64,
        image="localhost:5001/meridian-agent@sha256:" + "c" * 64,
        worker_pool="meridian/meridian",
        sandbox_class="gvisor",
        actor=actor,
        golden_tag="tag",
        golden_snapshot="golden",
    )


class _Preempting:
    """InProcessExecutor whose run raises WorkerLostError for the first `losses` attempts of
    (agent, cut); each plan names a new actor, as a real one does."""

    name = "substrate"

    def __init__(self, inner: InProcessExecutor, lose: dict[tuple[str, str], int]) -> None:
        self._inner = inner
        self._lose = dict(lose)
        self.actors: list[str] = []
        self.plans = 0

    def executor_name(self, agent_id: str) -> str:
        return self.name

    def plan(self, agent_id: str, trigger: Event) -> RunPlan:
        self.plans += 1
        return RunPlan(self.name, _placement(f"mer-{agent_id[-5:]}-{self.plans}"))

    def capacity(self, executor: str) -> int:
        return 2

    def run(self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None) -> Any:
        assert plan is not None
        assert plan.substrate is not None
        self.actors.append(plan.substrate.actor)
        left = self._lose.get((agent_id, cut_id), 0)
        if left:
            self._lose[(agent_id, cut_id)] = left - 1
            raise WorkerLostError(f"actor {plan.substrate.actor} lost its worker (preempted)")
        return self._inner.run(agent_id, trigger, cut_id)

    def drain(self) -> None:
        return None


def _pass(path: Path, lose: dict[tuple[str, str], int]) -> tuple[SqliteLedger, Any, _Preempting]:
    ledger = make_ledger(path, FixedClock(T0))
    for cut in CUTS:
        record_data_cut(
            ledger,
            clock=FixedClock(T0),
            actor="system.ingest",
            study_id=STUDY,
            cut_id=cut,
            path=Path(cut),
        )
    registry = AgentRegistry()
    for agent_id in AGENTS:
        registry.register(FakeAgent(agent_id, sites=["1001"]), AgentConfig())
    inner = InProcessExecutor(
        registry=registry, reader_factory=StubReader, log=structlog.get_logger()
    )
    executor = _Preempting(inner, lose)
    runner = AgentRunner(
        ledger=ledger,
        registry=registry,
        executor=executor,
        clock=FixedClock(T0),
        study_id=STUDY,
        log=structlog.get_logger(),
        operational_cuts=(),
    )
    return ledger, runner.run_pending(), executor


def _events(ledger: SqliteLedger) -> list[tuple[str, str, int, str | None]]:
    """(type, agent, trigger seq, planned actor) of every run event, in ledger order."""
    out: list[tuple[str, str, int, str | None]] = []
    for e in ledger.read():
        assert isinstance(e, Event)
        p = e.payload
        if isinstance(p, ActorWoke | ActorSuspended):
            actor = p.substrate.actor if p.substrate else None
            out.append((p.type.value, p.agent_id, p.trigger_seq, actor))
    return out


def test_a_preempted_run_is_retried_once_on_a_new_actor(tmp_path: Path) -> None:
    ledger, report, _ = _pass(tmp_path / "l.sqlite3", {("rbqm.bravo", "DC-A"): 1})
    with ledger:
        assert verify(ledger).ok
        assert report.failed == []
        assert [(a, s) for a, s, _ in report.retried] == [("rbqm.bravo", 1)]
        assert "preempted" in report.retried[0][2]
        assert len(report.completed) == len(AGENTS) * len(CUTS)
        events = _events(ledger)
        bravo_a = [
            (t, actor) for t, agent, seq, actor in events if (agent, seq) == ("rbqm.bravo", 1)
        ]
        # The lost attempt leaves its ActorWoke alone; the retry, on another actor, is
        # woke then suspended; both directly one after the other in the ledger.
        assert [t for t, _ in bravo_a] == ["actor_woke", "actor_woke", "actor_suspended"]
        lost, retry = bravo_a[0][1], bravo_a[1][1]
        assert lost is not None
        assert retry is not None
        assert lost != retry
        assert lost in report.retried[0][2]
        # The commit order is still (trigger seq, agent): alpha DC-A, bravo DC-A (both
        # attempts), then DC-B.
        woke = [(seq, agent) for t, agent, seq, _ in events if t == "actor_woke"]
        assert woke == [
            (1, "rbqm.alpha"),
            (1, "rbqm.bravo"),
            (1, "rbqm.bravo"),
            (2, "rbqm.alpha"),
            (2, "rbqm.bravo"),
        ]
        kinds = [e.payload.type.value for e in ledger.read() if isinstance(e, Event)]
        assert kinds.count(FindingRaised.model_fields["type"].default.value) == 4


def test_a_second_loss_fails_the_run_without_another_retry(tmp_path: Path) -> None:
    ledger, report, executor = _pass(tmp_path / "l.sqlite3", {("rbqm.alpha", "DC-B"): 2})
    with ledger:
        assert verify(ledger).ok
        assert [(a, s) for a, s, _ in report.retried] == [("rbqm.alpha", 2)]
        assert [(a, s) for a, s, _ in report.failed] == [("rbqm.alpha", 2)]
        assert len(report.completed) == len(AGENTS) * len(CUTS) - 1
        alpha_b = [e for e in _events(ledger) if e[1:3] == ("rbqm.alpha", 2)]
        assert [e[0] for e in alpha_b] == ["actor_woke", "actor_woke"]  # one retry only
        assert len(executor.actors) == len(AGENTS) * len(CUTS) + 1


def test_an_ordinary_failure_is_not_retried(tmp_path: Path) -> None:
    class _Boom(_Preempting):
        def run(
            self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None
        ) -> Any:
            if agent_id == "rbqm.alpha" and cut_id == "DC-A":
                self.actors.append("boom")
                raise RuntimeError("agent crashed")
            return super().run(agent_id, trigger, cut_id, plan)

    ledger = make_ledger(tmp_path / "l.sqlite3", FixedClock(T0))
    for cut in CUTS:
        record_data_cut(
            ledger,
            clock=FixedClock(T0),
            actor="system.ingest",
            study_id=STUDY,
            cut_id=cut,
            path=Path(cut),
        )
    registry = AgentRegistry()
    for agent_id in AGENTS:
        registry.register(FakeAgent(agent_id, sites=["1001"]), AgentConfig())
    inner = InProcessExecutor(
        registry=registry, reader_factory=StubReader, log=structlog.get_logger()
    )
    executor = _Boom(inner, {})
    runner = AgentRunner(
        ledger=ledger,
        registry=registry,
        executor=executor,
        clock=FixedClock(T0),
        study_id=STUDY,
        log=structlog.get_logger(),
        operational_cuts=(),
    )
    with ledger:
        report = runner.run_pending()
        assert report.retried == []
        assert [(a, s) for a, s, _ in report.failed] == [("rbqm.alpha", 1)]
        assert executor.actors.count("boom") == 1
