"""A rerun is recorded only when it produced something new (phase 4b step 3, item E)."""

import dataclasses
from pathlib import Path
from typing import Any

import structlog

from meridian.agents.base import AgentConfig
from meridian.agents.registry import AgentRegistry
from meridian.agents.runner import AgentRunner, RunPlan, drafts_sha256
from meridian.data.datacut import record_data_cut
from meridian.domain.events import ActorSuspended, ActorWoke, Event
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.sandbox.executor import InProcessExecutor
from tests.support import T0, make_ledger
from tests.unit.test_preemption import AGENTS, CUTS, _Preempting
from tests.unit.test_runner import STUDY, FakeAgent, StubReader


class _Variable(_Preempting):
    """_Preempting whose results carry a settable image digest, and that can fail."""

    image = "sha256:" + "1" * 64
    fail: bool = False

    def run(self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None) -> Any:
        if self.fail:
            raise RuntimeError("agent crashed")
        result = super().run(agent_id, trigger, cut_id, plan)
        return dataclasses.replace(result, image_digest=self.image)


def _setup(path: Path) -> tuple[SqliteLedger, AgentRunner, _Variable]:
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
    executor = _Variable(inner, {})
    runner = AgentRunner(
        ledger=ledger,
        registry=registry,
        executor=executor,
        clock=FixedClock(T0),
        study_id=STUDY,
        log=structlog.get_logger(),
        operational_cuts=(),
    )
    return ledger, runner, executor


def _runs(ledger: SqliteLedger) -> int:
    return sum(
        isinstance(e, Event) and isinstance(e.payload, ActorWoke | ActorSuspended)
        for e in ledger.read()
    )


def test_an_identical_rerun_appends_nothing_and_says_so(tmp_path: Path) -> None:
    ledger, runner, _ = _setup(tmp_path / "l.sqlite3")
    with ledger:
        first = runner.run_pending()
        assert len(first.completed) == 4
        size = len(ledger.read())
        again = runner.run_pending(rerun=True)
        assert again.completed == []
        assert sorted(again.unchanged) == sorted(first.completed)
        assert len(ledger.read()) == size  # not one event
        assert verify(ledger).ok


def test_a_rerun_with_another_image_is_recorded_once(tmp_path: Path) -> None:
    ledger, runner, executor = _setup(tmp_path / "l.sqlite3")
    with ledger:
        runner.run_pending()
        executor.image = "sha256:" + "2" * 64
        changed = runner.run_pending(rerun=True, cuts=["DC-A"])
        assert sorted(changed.completed) == [("rbqm.alpha", 1), ("rbqm.bravo", 1)]
        assert changed.unchanged == []
        assert changed.findings_appended == []  # the findings were already recorded
        assert len(changed.findings_skipped) == 2
        assert _runs(ledger) == 2 * 4 + 2 * 2
        # The same new image again: now it is a recorded run, so nothing new.
        same = runner.run_pending(rerun=True, cuts=["DC-A"])
        assert same.completed == []
        assert len(same.unchanged) == 2
        assert _runs(ledger) == 2 * 4 + 2 * 2


def test_a_failed_rerun_and_a_lost_worker_are_always_recorded(tmp_path: Path) -> None:
    ledger, runner, executor = _setup(tmp_path / "l.sqlite3")
    with ledger:
        runner.run_pending()
        before = _runs(ledger)
        executor.fail = True
        failed = runner.run_pending(rerun=True, cuts=["DC-B"])
        assert len(failed.failed) == 2
        assert _runs(ledger) == before + 2  # an ActorWoke each, as for any failed run
        executor.fail = False
        executor._lose = {("rbqm.alpha", "DC-B"): 1}
        lost = runner.run_pending(rerun=True, cuts=["DC-B"])
        # alpha: the lost attempt and its retry are new; bravo: identical, not recorded.
        assert [(a, s) for a, s, _ in lost.retried] == [("rbqm.alpha", 2)]
        assert lost.completed == [("rbqm.alpha", 2)]
        assert lost.unchanged == [("rbqm.bravo", 2)]
        assert _runs(ledger) == before + 2 + 3
        assert verify(ledger).ok


def test_the_drafts_hash_is_recorded_and_follows_the_drafts(tmp_path: Path) -> None:
    ledger, runner, _ = _setup(tmp_path / "l.sqlite3")
    with ledger:
        runner.run_pending()
        hashes = {
            e.payload.drafts_sha256
            for e in ledger.read()
            if isinstance(e, Event) and isinstance(e.payload, ActorSuspended)
        }
    assert None not in hashes
    assert len(hashes) == 4  # each (agent, cut) run drafted something different
    assert drafts_sha256(()) == drafts_sha256([])
