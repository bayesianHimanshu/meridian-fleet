"""var/sandboxes.jsonl, the background deleter and meridian sandbox reap."""

import threading
from pathlib import Path

import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.runner import AgentRunner
from meridian.data.datacut import data_cut_events, record_data_cut
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import FixedClock
from meridian.sandbox.openshell import ExecOutput, OpenShellError
from meridian.sandbox.tracking import (
    BackgroundDeleter,
    SandboxTracker,
    TrackingError,
    reap,
)
from tests.rbqm_support import NOW, STUDY
from tests.support import make_ledger
from tests.unit.test_executor import (
    AGENTS,
    FakeOpenShellClient,
    _executor,
    _expected,
    _expected_for,
)
from tests.worker_support import cut_trigger, tiny_cut

LOG = structlog.get_logger()


def _tracker(tmp_path: Path) -> SandboxTracker:
    return SandboxTracker(tmp_path / "var" / "sandboxes.jsonl", FixedClock(NOW))


class GatedDelete:
    """A delete that blocks until released, so we can see who waits for it."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.done: list[str] = []
        self.fail = fail or set()
        self._lock = threading.Lock()

    def __call__(self, name: str) -> None:
        self.started.set()
        assert self.release.wait(10), "test never released the deleter"
        if name in self.fail:
            raise OpenShellError(f"cannot delete {name}")
        with self._lock:
            self.done.append(name)


def test_every_submitted_sandbox_ends_deleted_or_delete_failed(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    gate = GatedDelete(fail={"mer-cs-2-bbbbbbbb"})
    deleter = BackgroundDeleter(delete=gate, tracker=tracker, workers=2, log=LOG)
    names = ["mer-cs-1-aaaaaaaa", "mer-cs-2-bbbbbbbb", "mer-cs-3-cccccccc"]
    for name in names:
        tracker.record(name, "created")
        deleter.submit(name)  # returns at once: deletion is in the background
    assert set(tracker.latest().values()) == {"delete_requested"}
    gate.release.set()
    report = deleter.drain()
    assert sorted(report.deleted) == [names[0], names[2]]
    assert report.failed == [names[1]]
    assert tracker.latest() == {
        names[0]: "deleted",
        names[1]: "delete_failed",
        names[2]: "deleted",
    }
    assert tracker.pending() == [names[1]]
    deleter.close()


def test_zero_workers_deletes_synchronously(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    deleted: list[str] = []
    deleter = BackgroundDeleter(delete=deleted.append, tracker=tracker, workers=0, log=LOG)
    tracker.record("mer-cs-1-aaaaaaaa", "created")
    deleter.submit("mer-cs-1-aaaaaaaa")
    assert deleted == ["mer-cs-1-aaaaaaaa"]
    assert tracker.pending() == []


def test_executor_returns_before_the_delete_and_the_pass_waits(tmp_path: Path) -> None:
    cut_dir = tiny_cut(tmp_path / "curated")
    client = FakeOpenShellClient(output=ExecOutput(0, _expected(cut_dir), ""))
    gate = GatedDelete()
    executor = _executor(client, cut_dir, workers=2)
    executor._deleter = BackgroundDeleter(  # gate the real deletion
        delete=gate, tracker=executor._tracker, workers=2, log=LOG
    )
    result = executor.run("rbqm.csm", cut_trigger(), "DC-TINY")
    assert result.drafts is not None
    assert gate.started.wait(5)
    assert gate.done == []  # the run has returned while the delete is still blocked

    ledger = make_ledger(tmp_path / "l.sqlite3", FixedClock(NOW))
    record_data_cut(
        ledger,
        clock=FixedClock(NOW),
        actor="system.ingest",
        study_id=STUDY,
        cut_id="DC-TINY",
        path=cut_dir,
    )
    runner = AgentRunner(
        ledger=ledger,
        registry=build_registry(AGENTS.model_copy(update={"enabled": ("rbqm.csm",)})),
        executor=executor,
        clock=FixedClock(NOW),
        study_id=STUDY,
        log=LOG,
    )
    client.output = ExecOutput(0, _expected_for_ledger(cut_dir, ledger), "")
    timer = threading.Timer(0.3, gate.release.set)
    timer.start()
    report = runner.run_pending()
    timer.join()
    # run_pending returned only after both deletions finished.
    assert report.completed
    assert sorted(gate.done) == sorted(client.created)
    assert executor._tracker.pending() == []
    ledger.close()


def _expected_for_ledger(cut_dir: Path, ledger: SqliteLedger) -> str:
    (event,) = data_cut_events(ledger, STUDY)
    return _expected_for(cut_dir, event)


def test_reap_deletes_only_tracked_undeleted_sandboxes(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.record("mer-a", "created")
    tracker.record("mer-a", "delete_requested")
    tracker.record("mer-a", "deleted")  # done: never touched again
    tracker.record("mer-b", "created")  # kept after a failure
    tracker.record("mer-c", "created")
    tracker.record("mer-c", "delete_requested")
    tracker.record("mer-c", "delete_failed", "gateway busy")
    tracker.record("mer-d", "created")  # gone already (for example deleted by hand)
    live = ["mer-a", "mer-b", "mer-c", "someone-elses-sandbox"]
    deleted: list[str] = []

    def delete(name: str) -> None:
        deleted.append(name)
        live.remove(name)

    report = reap(tracker=tracker, delete=delete, live=lambda: list(live), log=LOG)
    assert deleted == ["mer-b", "mer-c"]
    assert report.deleted == ["mer-b", "mer-c"]
    assert report.already_gone == ["mer-d"]
    assert tracker.pending() == []
    assert "someone-elses-sandbox" not in tracker.latest()
    assert "someone-elses-sandbox" in live
    # A second reap has nothing to do and asks for nothing.
    calls: list[str] = []
    again = reap(
        tracker=tracker,
        delete=calls.append,
        live=lambda: pytest.fail("live() not needed"),
        log=LOG,
    )
    assert (again.deleted, again.already_gone, calls) == ([], [], [])


def test_reap_records_a_failed_delete_and_keeps_it_pending(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.record("mer-b", "created")

    def refuse(name: str) -> None:
        raise OpenShellError("no")

    report = reap(tracker=tracker, delete=refuse, live=lambda: ["mer-b"], log=LOG)
    assert report.failed == ["mer-b"]
    stuck = reap(
        tracker=tracker,
        delete=lambda _: None,
        live=lambda: ["mer-b"],
        log=LOG,
        confirm_seconds=0.05,
        poll_seconds=0.01,
    )
    assert stuck.failed == ["mer-b"]
    assert report.failed == ["mer-b"]
    assert tracker.pending() == ["mer-b"]


def test_the_jsonl_is_append_only(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.record("mer-a", "created")
    first = tracker.path.read_bytes()
    deleter = BackgroundDeleter(delete=lambda _: None, tracker=tracker, workers=1, log=LOG)
    deleter.submit("mer-a")
    deleter.close()
    reap(tracker=tracker, delete=lambda _: None, live=lambda: [], log=LOG)
    after = tracker.path.read_bytes()
    assert after.startswith(first)
    lines = after.decode().splitlines()
    assert [line.count('"event"') for line in lines] == [1] * len(lines)
    assert [e["event"] for e in tracker.events()] == ["created", "delete_requested", "deleted"]


def test_a_corrupt_line_is_an_error_not_silently_skipped(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.record("mer-a", "created")
    with tracker.path.open("a") as handle:
        handle.write("not json\n")
    with pytest.raises(TrackingError, match=":2 is not JSON"):
        tracker.pending()


def test_the_executor_tracks_kept_and_failed_sandboxes(tmp_path: Path) -> None:
    cut_dir = tiny_cut(tmp_path / "curated")
    client = FakeOpenShellClient(output=ExecOutput(4, "", "boom"))
    executor = _executor(client, cut_dir, keep_failed=True)
    with pytest.raises(Exception, match="exited 4"):
        executor.run("rbqm.csm", cut_trigger(), "DC-TINY")
    (name,) = client.created
    assert executor._tracker.latest() == {name: "created"}
    assert executor._tracker.pending() == [name]  # reap will find it


def test_deleted_means_no_longer_listed(tmp_path: Path) -> None:
    """delete returns at "cleanup is pending"; deleted is recorded only once it is gone."""
    tracker = _tracker(tmp_path)
    listings = iter([["mer-a"], ["mer-a"], []])
    deleter = BackgroundDeleter(
        delete=lambda _: None,
        tracker=tracker,
        workers=1,
        log=LOG,
        live=lambda: next(listings),
        poll_seconds=0.01,
    )
    tracker.record("mer-a", "created")
    deleter.submit("mer-a")
    assert deleter.drain().deleted == ["mer-a"]
    assert tracker.latest() == {"mer-a": "deleted"}
    deleter.close()


def test_still_listed_after_the_timeout_is_delete_failed(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    deleter = BackgroundDeleter(
        delete=lambda _: None,
        tracker=tracker,
        workers=1,
        log=LOG,
        live=lambda: ["mer-a"],
        confirm_seconds=0.05,
        poll_seconds=0.01,
    )
    tracker.record("mer-a", "created")
    deleter.submit("mer-a")
    assert deleter.drain().failed == ["mer-a"]
    (last,) = [e for e in tracker.events() if e["event"] == "delete_failed"]
    assert "still listed" in str(last["detail"])
    assert tracker.pending() == ["mer-a"]
    deleter.close()
