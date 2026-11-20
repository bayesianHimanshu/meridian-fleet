"""Every sandbox this code creates, in var/sandboxes.jsonl, and how each is torn down.

The file is append-only: one JSON line per event (created, delete_requested, deleted,
delete_failed). A sandbox is pending until its latest event is deleted. Deletion runs on
a bounded thread pool so a run returns as soon as the worker's output is read; the runner
drains the pool before a pass ends. `meridian sandbox reap` deletes pending sandboxes,
and only those.
"""

import json
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog

from meridian.ledger.hashing import canonical_json, format_utc
from meridian.platform.clock import Clock
from meridian.sandbox.openshell import OpenShellError

TrackEvent = Literal["created", "delete_requested", "deleted", "delete_failed"]
EVENTS: tuple[str, ...] = ("created", "delete_requested", "deleted", "delete_failed")


class TrackingError(Exception):
    pass


class SandboxTracker:
    """Appends to the jsonl file; never rewrites or truncates it."""

    def __init__(self, path: Path, clock: Clock) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, sandbox: str, event: TrackEvent, detail: str | None = None) -> None:
        line = canonical_json(
            {
                "at": format_utc(self._clock.now()),
                "detail": detail,
                "event": event,
                "sandbox": sandbox,
            }
        )
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def events(self) -> list[dict[str, str | None]]:
        if not self._path.exists():
            return []
        out: list[dict[str, str | None]] = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise TrackingError(f"{self._path}:{number} is not JSON") from exc
            if item.get("event") not in EVENTS or not isinstance(item.get("sandbox"), str):
                raise TrackingError(f"{self._path}:{number} is not a tracking record")
            out.append(item)
        return out

    def latest(self) -> dict[str, str]:
        """sandbox name to its latest event, in the order the sandboxes first appear."""
        state: dict[str, str] = {}
        for item in self.events():
            state[str(item["sandbox"])] = str(item["event"])
        return state

    def pending(self) -> list[str]:
        return [name for name, event in self.latest().items() if event != "deleted"]


Deleter = Callable[[str], None]


def wait_gone(
    name: str, live: Callable[[], list[str]], seconds: float, poll_seconds: float = 0.5
) -> bool:
    """True once `name` is no longer listed; False if it still is after `seconds`."""
    deadline = time.monotonic() + seconds
    waiter = threading.Event()
    while True:
        if name not in live():
            return True
        if time.monotonic() >= deadline:
            return False
        waiter.wait(poll_seconds)


@dataclass
class DrainReport:
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


class BackgroundDeleter:
    """Deletes sandboxes off the critical path. workers = 0 deletes synchronously.

    `openshell sandbox delete` (0.1.7) returns once the gateway has accepted the deletion
    ("cleanup is pending"); the sandbox can stay listed for a while. With `live` given, a
    sandbox is recorded as deleted only once it is no longer listed, polling every
    `poll_seconds` for at most `confirm_seconds`; after that it is delete_failed
    ("still listed") and meridian sandbox reap will try again.
    """

    def __init__(
        self,
        *,
        delete: Deleter,
        tracker: SandboxTracker,
        workers: int,
        log: structlog.typing.FilteringBoundLogger,
        live: Callable[[], list[str]] | None = None,
        confirm_seconds: float = 60.0,
        poll_seconds: float = 0.5,
    ) -> None:
        if workers < 0:
            raise ValueError("workers must be 0 or more")
        self._live = live
        self._confirm_seconds = confirm_seconds
        self._poll_seconds = poll_seconds
        self._delete = delete
        self._tracker = tracker
        self._log = log
        self._pool = ThreadPoolExecutor(workers, "sandbox-delete") if workers else None
        self._futures: list[Future[tuple[str, bool]]] = []
        self._lock = threading.Lock()
        self._report = DrainReport()

    def _gone(self, name: str) -> bool:
        if self._live is None:
            return True
        return wait_gone(name, self._live, self._confirm_seconds, self._poll_seconds)

    def _run(self, name: str) -> tuple[str, bool]:
        try:
            self._delete(name)
            gone = self._gone(name)
        except OpenShellError as exc:
            self._tracker.record(name, "delete_failed", str(exc)[:500])
            self._log.error("sandbox.delete_failed", sandbox=name, error=str(exc))
            return name, False
        if not gone:
            detail = f"still listed {self._confirm_seconds:g} s after delete"
            self._tracker.record(name, "delete_failed", detail)
            self._log.error("sandbox.delete_failed", sandbox=name, error=detail)
            return name, False
        self._tracker.record(name, "deleted")
        return name, True

    def submit(self, name: str) -> None:
        self._tracker.record(name, "delete_requested")
        if self._pool is None:
            self._settle(self._run(name))
            return
        with self._lock:
            self._futures.append(self._pool.submit(self._run, name))

    def _settle(self, outcome: tuple[str, bool]) -> None:
        name, ok = outcome
        (self._report.deleted if ok else self._report.failed).append(name)

    def drain(self) -> DrainReport:
        """Wait for every submitted deletion; return what happened since the last drain."""
        with self._lock:
            futures, self._futures = self._futures, []
        for future in futures:
            self._settle(future.result())
        report, self._report = self._report, DrainReport()
        return report

    def close(self) -> None:
        self.drain()
        if self._pool is not None:
            self._pool.shutdown(wait=True)


@dataclass
class ReapReport:
    deleted: list[str] = field(default_factory=list)
    already_gone: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def reap(
    *,
    tracker: SandboxTracker,
    delete: Deleter,
    live: Callable[[], list[str]],
    log: structlog.typing.FilteringBoundLogger,
    confirm_seconds: float = 60.0,
    poll_seconds: float = 0.5,
) -> ReapReport:
    """Delete every tracked sandbox whose latest event is not deleted, and nothing else.

    A tracked sandbox that no longer exists is recorded as deleted (detail "not found").
    """
    report = ReapReport()
    pending = tracker.pending()
    if not pending:
        return report
    existing = set(live())
    for name in pending:
        if name not in existing:
            tracker.record(name, "deleted", "not found")
            report.already_gone.append(name)
            continue
        tracker.record(name, "delete_requested", "reap")
        try:
            delete(name)
            gone = wait_gone(name, live, confirm_seconds, poll_seconds)
        except OpenShellError as exc:
            tracker.record(name, "delete_failed", str(exc)[:500])
            log.error("sandbox.reap_failed", sandbox=name, error=str(exc))
            report.failed.append(name)
            continue
        if not gone:
            tracker.record(
                name, "delete_failed", f"still listed {confirm_seconds:g} s after delete"
            )
            report.failed.append(name)
            continue
        tracker.record(name, "deleted", "reap")
        report.deleted.append(name)
    return report
