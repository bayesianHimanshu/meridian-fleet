"""The parallel runner (phase 4a step 3, item 2): concurrent runs, capped per executor,
queued beyond the cap, and appended by the host in (trigger seq, agent) order so that the
same pass gives the same ledger whatever the timing."""

import random
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from meridian.agents.base import AgentConfig
from meridian.agents.registry import AgentRegistry
from meridian.agents.runner import AgentRunner, RunPlan
from meridian.data.datacut import record_data_cut
from meridian.domain.events import ActorSuspended, ActorWoke, Event, FindingRaised
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock, SystemClock
from meridian.sandbox.executor import InProcessExecutor
from tests.support import T0, make_ledger
from tests.unit.test_runner import STUDY, FakeAgent, StubReader

AGENTS = ("rbqm.alpha", "rbqm.bravo", "rbqm.charlie", "rbqm.delta")
CUTS = ("DC-A", "DC-B", "DC-C")


class _Jittery:
    """InProcessExecutor that takes a random time per run and counts runs in flight."""

    name = "substrate"

    def __init__(self, inner: InProcessExecutor, capacity: int, seed: int, fail: str = "") -> None:
        self._inner = inner
        self._capacity = capacity
        self._random = random.Random(seed)
        self._lock = threading.Lock()
        self._fail = fail
        self.in_flight = 0
        self.max_in_flight = 0
        self.finished: list[tuple[str, int]] = []

    def executor_name(self, agent_id: str) -> str:
        return self.name

    def plan(self, agent_id: str, trigger: Event) -> RunPlan:
        return RunPlan(self.name)

    def capacity(self, executor: str) -> int:
        return self._capacity

    def run(self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None) -> Any:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            delay = self._random.uniform(0, 0.02)
        try:
            time.sleep(delay)
            if agent_id == self._fail and cut_id == "DC-B":
                raise RuntimeError("boom")
            return self._inner.run(agent_id, trigger, cut_id)
        finally:
            with self._lock:
                self.in_flight -= 1
                self.finished.append((agent_id, trigger.seq))

    def drain(self) -> None:
        return None


def _pass(path: Path, capacity: int, seed: int, fail: str = "") -> tuple[list[Any], _Jittery]:
    with make_ledger(path, FixedClock(T0)) as ledger:
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
        # Registered out of order: the commit order must not depend on it.
        for agent_id in reversed(AGENTS):
            registry.register(FakeAgent(agent_id, sites=["1001", "1002"]), AgentConfig())
        inner = InProcessExecutor(
            registry=registry, reader_factory=StubReader, log=structlog.get_logger()
        )
        executor = _Jittery(inner, capacity, seed, fail)
        runner = AgentRunner(
            ledger=ledger,
            registry=registry,
            executor=executor,
            clock=FixedClock(T0),
            study_id=STUDY,
            log=structlog.get_logger(),
            operational_cuts=(),
        )
        report = runner.run_pending()
        assert report.executors == {"substrate": len(AGENTS) * len(CUTS)}
        assert report.capacity == {"substrate": capacity}
        assert verify(ledger).ok
        return [_content(e.model_dump(mode="json")) for e in ledger.read()], executor


def _content(event: dict[str, Any]) -> dict[str, Any]:
    """Everything but the event id (a fresh uuid7 per append) and the hashes it feeds."""
    return {k: v for k, v in event.items() if k not in ("event_id", "prev_hash", "hash")}


def test_the_same_pass_gives_the_same_ledger_however_runs_are_timed(tmp_path: Path) -> None:
    sequential, _ = _pass(tmp_path / "seq.sqlite3", capacity=1, seed=0)
    for seed in range(1, 6):
        parallel, executor = _pass(tmp_path / f"par{seed}.sqlite3", capacity=4, seed=seed)
        assert executor.max_in_flight > 1  # runs really overlapped
        assert executor.finished != sorted(executor.finished)  # and finished out of order
        assert parallel == sequential  # seq, times, actor, visibility and payload


def test_runs_are_appended_by_trigger_seq_then_agent(tmp_path: Path) -> None:
    events, _ = _pass(tmp_path / "l.sqlite3", capacity=3, seed=7)
    woke = [
        (e["payload"]["trigger_seq"], e["payload"]["agent_id"])
        for e in events
        if e["payload"]["type"] == ActorWoke.model_fields["type"].default.value
    ]
    assert woke == sorted(woke)
    assert len(woke) == len(AGENTS) * len(CUTS)
    # Each run's events are contiguous: ActorWoke, its findings, ActorSuspended.
    kinds = [e["payload"]["type"] for e in events[len(CUTS) :]]
    run = [
        ActorWoke.model_fields["type"].default.value,
        FindingRaised.model_fields["type"].default.value,
        FindingRaised.model_fields["type"].default.value,
        ActorSuspended.model_fields["type"].default.value,
    ]
    assert kinds == run * (len(AGENTS) * len(CUTS))


def test_runs_beyond_the_capacity_wait_instead_of_failing(tmp_path: Path) -> None:
    _, executor = _pass(tmp_path / "l.sqlite3", capacity=2, seed=3)
    assert executor.max_in_flight == 2
    assert len(executor.finished) == len(AGENTS) * len(CUTS)


def test_a_failed_run_keeps_its_place_and_the_rest_are_unchanged(tmp_path: Path) -> None:
    sequential, _ = _pass(tmp_path / "seq.sqlite3", capacity=1, seed=0, fail="rbqm.bravo")
    parallel, _ = _pass(tmp_path / "par.sqlite3", capacity=4, seed=9, fail="rbqm.bravo")
    assert parallel == sequential
    failed = [
        i
        for i, e in enumerate(parallel)
        if e["payload"]["type"] == ActorWoke.model_fields["type"].default.value
        and e["payload"]["agent_id"] == "rbqm.bravo"
        and parallel[i + 1]["payload"]["type"] == ActorWoke.model_fields["type"].default.value
    ]
    assert len(failed) == 1  # only ActorWoke for the failed run, then the next run


def test_the_ledger_is_written_only_from_the_calling_thread(tmp_path: Path) -> None:
    writers: set[str] = set()
    original = SqliteLedger.append

    def append(self: SqliteLedger, draft: Any) -> Any:
        writers.add(threading.current_thread().name)
        return original(self, draft)

    SqliteLedger.append = append  # type: ignore[method-assign]
    try:
        _pass(tmp_path / "l.sqlite3", capacity=4, seed=5)
    finally:
        SqliteLedger.append = original  # type: ignore[method-assign]
    assert writers == {threading.current_thread().name}


class _Timed(_Jittery):
    """_Jittery that records when each run really started and ended."""

    def __init__(self, inner: InProcessExecutor, capacity: int, seed: int) -> None:
        super().__init__(inner, capacity, seed)
        self.started: dict[tuple[str, int], datetime] = {}
        self.ended: dict[tuple[str, int], datetime] = {}

    def run(self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None) -> Any:
        self.started[(agent_id, trigger.seq)] = datetime.now(UTC)
        try:
            return super().run(agent_id, trigger, cut_id, plan)
        finally:
            self.ended[(agent_id, trigger.seq)] = datetime.now(UTC)


def test_actor_woke_carries_the_runs_real_start_not_its_commit(tmp_path: Path) -> None:
    """ActorWoke is appended after the run, so its own time is the commit. Its payload's
    started_at is when the run left the queue: before the run began, and before it ended."""
    with make_ledger(tmp_path / "l.sqlite3", SystemClock()) as ledger:
        for cut in CUTS:
            record_data_cut(
                ledger,
                clock=SystemClock(),
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
        executor = _Timed(inner, capacity=2, seed=11)  # half the runs wait in the queue
        AgentRunner(
            ledger=ledger,
            registry=registry,
            executor=executor,
            clock=SystemClock(),
            study_id=STUDY,
            log=structlog.get_logger(),
            operational_cuts=(),
        ).run_pending()
        woke = [
            e for e in ledger.read() if isinstance(e, Event) and isinstance(e.payload, ActorWoke)
        ]
    assert len(woke) == len(AGENTS) * len(CUTS)
    for e in woke:
        assert isinstance(e.payload, ActorWoke)
        key = (e.payload.agent_id, e.payload.trigger_seq)
        started = e.payload.started_at
        assert started is not None
        assert started <= executor.started[key] < started + timedelta(seconds=1)
        assert started < executor.ended[key] <= e.occurred_at  # appended after the run
    # Queued runs started later than the first ones, which the commit time cannot show.
    starts = sorted(
        e.payload.started_at
        for e in woke
        if isinstance(e.payload, ActorWoke) and e.payload.started_at is not None
    )
    assert starts[-1] > starts[0]
