"""One runner pass: wake each enabled agent once per DataCut and record what it finds.

A pass runs independent (agent, DataCut) runs concurrently, as many at a time per executor
as its capacity() allows (for substrate, the pool's free workers); the rest wait in that
executor's queue. The ledger stays single writer: runs never append, the host does, in
one order fixed before anything runs (trigger seq, then agent id). Each run's events
(ActorWoke, its findings, ActorSuspended) are appended together once it and every run
before it in that order have finished, so a pass gives the same ledger content however
long each run takes. ActorWoke is therefore appended after its run, not before, and
carries the run's real start in started_at.
"""

from collections.abc import Callable, Collection, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import structlog

from meridian.agents.base import FindingDraft
from meridian.agents.registry import AgentRegistry, RegisteredAgent
from meridian.data.datacut import Ledger, data_cut_events
from meridian.domain.audit import LEDGER_SCOPE
from meridian.domain.dedup import dedup_prefix
from meridian.domain.enums import EventType, Role, Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    AuditReviewRequested,
    DataCut,
    Event,
    EventDraft,
    EventPayload,
    FindingRaised,
    LedgerEntry,
    SubstrateRun,
)
from meridian.domain.findings import Finding
from meridian.ledger.store import DuplicateDedupKeyError
from meridian.platform.clock import Clock


class RunOutcome(Protocol):
    """What an executor returns (meridian.sandbox.executor.ExecutionResult)."""

    @property
    def drafts(self) -> tuple[FindingDraft, ...]: ...

    @property
    def image_digest(self) -> str | None: ...

    @property
    def policy_sha256(self) -> str | None: ...

    @property
    def inputs_manifest_sha256(self) -> str | None: ...

    @property
    def policy_file(self) -> str | None: ...

    @property
    def policy_file_sha256(self) -> str | None: ...

    @property
    def substrate(self) -> SubstrateRun | None: ...


@dataclass(frozen=True, slots=True)
class RunPlan:
    """What an executor knows before a run starts, recorded on ActorWoke."""

    executor: str
    substrate: SubstrateRun | None = None


class AgentExecutor(Protocol):
    """Where a run happens (meridian.sandbox.executor.Executor)."""

    @property
    def name(self) -> str: ...

    def executor_name(self, agent_id: str) -> str:
        """The executor a run of this agent goes to (a zoned executor picks per policy)."""
        ...

    def plan(self, agent_id: str, trigger: Event) -> RunPlan: ...

    def capacity(self, executor: str) -> int:
        """How many runs may go to the named executor at once (at least 1)."""
        ...

    def run(
        self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None
    ) -> RunOutcome: ...

    def drain(self) -> None:
        """Block until background work (sandbox deletion) has finished."""
        ...


AUDIT_ACTOR = "meridian.audit"  # the host component that requests an audit review


class SandboxRequiredError(RuntimeError):
    """An operational cut is pending and the executor is not the one config requires."""


@dataclass(slots=True)
class RunReport:
    completed: list[tuple[str, int]] = field(default_factory=list)
    failed: list[tuple[str, int, str]] = field(default_factory=list)
    findings_appended: list[str] = field(default_factory=list)
    findings_skipped: list[str] = field(default_factory=list)
    executors: dict[str, int] = field(default_factory=dict)  # runs sent to each executor
    capacity: dict[str, int] = field(default_factory=dict)  # runs at a time, per executor


@dataclass(frozen=True, slots=True)
class _Planned:
    registered: RegisteredAgent
    trigger: Event
    cut_id: str
    plan: RunPlan


class AgentRunner:
    def __init__(
        self,
        *,
        ledger: Ledger,
        registry: AgentRegistry,
        executor: AgentExecutor,
        clock: Clock,
        study_id: str,
        log: structlog.typing.FilteringBoundLogger,
        operational_cuts: Collection[str],
        operational_executors: Collection[str] = ("openshell", "substrate"),
    ) -> None:
        """operational_cuts ([cuts] operational) may only be processed by one of
        operational_executors ([cuts] operational_executors), decided per run (a zoned
        executor picks per policy): run_pending refuses any other executor for them
        before it records anything."""
        self._operational = frozenset(operational_cuts)
        self._operational_executors = tuple(operational_executors)
        self._ledger = ledger
        self._registry = registry
        self._executor = executor
        self._clock = clock
        self._study_id = study_id
        self._log = log

    def _completed(self) -> set[tuple[str, str | None, int]]:
        """(agent_id, agent_version, trigger_seq) of every finished run.

        Legacy ActorSuspended events have agent_version None, so they never mark a
        versioned agent's run as done: a new version re-runs every cut.
        """
        done: set[tuple[str, str | None, int]] = set()
        for entry in self._ledger.read(event_types=[EventType.ACTOR_SUSPENDED]):
            if isinstance(entry, Event) and isinstance(entry.payload, ActorSuspended):
                p = entry.payload
                done.add((p.agent_id, p.agent_version, p.trigger_seq))
        return done

    def _append(self, actor: str, visibility: Visibility, payload: EventPayload) -> Event:
        return self._ledger.append(
            EventDraft(
                occurred_at=self._clock.now(),
                actor=actor,
                study_id=self._study_id,
                visibility=visibility,
                payload=payload,
            )
        )

    def run_pending(
        self,
        agents: Collection[str] | None = None,
        cuts: Collection[str] | None = None,
        rerun: bool = False,
    ) -> RunReport:
        """Every pending (enabled agent, DataCut) pair; optionally only some agents and
        cuts (the rest stay pending for a later pass). rerun also runs pairs already
        completed (for example DC-BASE again after the executor of a zone changed): their
        findings are skipped as duplicates and the new run events record the executor."""
        report = RunReport()
        triggers = [
            t
            for t in data_cut_events(self._ledger, self._study_id)
            if cuts is None or (isinstance(t.payload, DataCut) and t.payload.cut_id in cuts)
        ]
        done = self._completed()
        pending = [
            (registered, trigger)
            for registered in self._registry.enabled()
            if EventType.DATA_CUT in registered.agent.subscribes_to
            and (agents is None or registered.agent.agent_id in agents)
            for trigger in triggers
            if rerun
            or (registered.agent.agent_id, registered.agent.version, trigger.seq) not in done
        ]
        pending.sort(key=lambda pair: (pair[1].seq, pair[0].agent.agent_id))
        self._require_sandbox(pending)
        planned: list[_Planned] = []
        for registered, trigger in pending:
            assert isinstance(trigger.payload, DataCut)
            plan = self._plan(registered, trigger, report)
            if plan is not None:
                planned.append(_Planned(registered, trigger, trigger.payload.cut_id, plan))
        self._run_concurrently(planned, report)
        self._executor.drain()  # a pass ends only when every sandbox is torn down
        return report

    def _plan(
        self, registered: RegisteredAgent, trigger: Event, report: RunReport
    ) -> RunPlan | None:
        agent = registered.agent
        try:
            return self._executor.plan(agent.agent_id, trigger)
        except Exception as exc:  # no template, no golden snapshot: nothing is recorded
            self._log.error(
                "agent.plan_failed",
                agent_id=agent.agent_id,
                trigger_seq=trigger.seq,
                error=f"{type(exc).__name__}: {exc}",
            )
            report.failed.append((agent.agent_id, trigger.seq, f"{type(exc).__name__}: {exc}"))
            return None

    def _run_concurrently(self, planned: Sequence[_Planned], report: RunReport) -> None:
        """One queue per executor, capacity() runs at a time each; commit in plan order."""
        names = sorted({p.plan.executor for p in planned})
        capacity = {n: max(1, self._executor.capacity(n)) for n in names}
        report.capacity.update(capacity)
        pools = {n: ThreadPoolExecutor(capacity[n], thread_name_prefix=f"run-{n}") for n in names}
        try:
            futures: list[Future[tuple[datetime, RunOutcome | Exception]]] = []
            for p in planned:
                report.executors[p.plan.executor] = report.executors.get(p.plan.executor, 0) + 1
                futures.append(pools[p.plan.executor].submit(self._timed_run, p))
            for p, future in zip(planned, futures, strict=True):
                started_at, outcome = future.result()
                self._commit(p, outcome, report, started_at=started_at)
        finally:
            for pool in pools.values():
                pool.shutdown(wait=True)

    def _timed_run(self, p: _Planned) -> tuple[datetime, RunOutcome | Exception]:
        """Run one planned run; return when it started (read as it leaves the queue) and its
        outcome or the exception it raised. ActorWoke is appended later, at commit, and
        carries this start time."""
        started_at = self._clock.now()
        try:
            return started_at, self._executor.run(
                p.registered.agent.agent_id, p.trigger, p.cut_id, p.plan
            )
        except Exception as exc:  # an agent failure must never stop the pass
            return started_at, exc

    def _allowed(self) -> str:
        return " or ".join(self._operational_executors)

    def _require_sandbox(self, pending: list[tuple[RegisteredAgent, Event]]) -> None:
        """Refuse the whole pass, before any event, if an operational cut is pending for
        a run whose executor is not one of the allowed ones."""
        refused = sorted(
            {
                (t.payload.cut_id, r.agent.agent_id, executor)
                for r, t in pending
                if isinstance(t.payload, DataCut)
                and t.payload.cut_id in self._operational
                and (executor := self._executor.executor_name(r.agent.agent_id))
                not in self._operational_executors
            }
        )
        if refused:
            cuts = sorted({c for c, _, _ in refused})
            pairs = ", ".join(f"{a} on {c} ({e})" for c, a, e in refused)
            raise SandboxRequiredError(
                f"operational cut(s) {', '.join(cuts)} may only be processed by the "
                f"{self._allowed()} executor ([cuts] operational_executors); pending: "
                f"{pairs}. Nothing was run. Use --executor openshell or zoned, or limit the "
                "pass to evaluation cuts."
            )

    def run_audit_review(
        self,
        prepare: Callable[[Event, Sequence[LedgerEntry]], object],
        through_seq: int | None = None,
    ) -> RunReport:
        """One audit review: append its trigger (AuditReviewRequested, seq 1 through
        through_seq, by default every event before it), let prepare write the audit
        extract for that trigger, and run every enabled agent that wakes on it. The ledger
        is operational data: only the operational executor may run it."""
        reviewers = [
            r
            for r in self._registry.enabled()
            if EventType.AUDIT_REVIEW_REQUESTED in r.agent.subscribes_to
        ]
        for r in reviewers:
            executor = self._executor.executor_name(r.agent.agent_id)
            if executor not in self._operational_executors:
                raise SandboxRequiredError(
                    f"an audit review reads the operational ledger and may only run in the "
                    f"{self._allowed()} executor, not {executor}. Nothing was run."
                )
        report = RunReport()
        if not reviewers:
            return report
        entries = self._ledger.read(viewer_role=Role.UNBLINDED_STATISTICIAN)
        last = entries[-1].seq if entries else 0
        through = last if through_seq is None else through_seq
        if not 1 <= through <= last:
            raise ValueError(f"through_seq {through} is outside the ledger (1 to {last})")
        trigger = self._append(
            AUDIT_ACTOR,
            Visibility.BLINDED,
            AuditReviewRequested(study_id=self._study_id, from_seq=1, through_seq=through),
        )
        prepare(trigger, entries)
        for registered in reviewers:
            self._run_one(registered, trigger, report, LEDGER_SCOPE, (1, through))
        self._executor.drain()
        return report

    def _run_one(
        self,
        registered: RegisteredAgent,
        trigger: Event,
        report: RunReport,
        cut_id: str,
        review: tuple[int, int],
    ) -> None:
        """One audit review run, in the calling thread."""
        plan = self._plan(registered, trigger, report)
        if plan is None:
            return
        p = _Planned(registered, trigger, cut_id, plan)
        report.executors[plan.executor] = report.executors.get(plan.executor, 0) + 1
        started_at, outcome = self._timed_run(p)
        self._commit(p, outcome, report, review, started_at=started_at)

    def _commit(
        self,
        p: _Planned,
        outcome: RunOutcome | Exception,
        report: RunReport,
        review: tuple[int, int] | None = None,
        *,
        started_at: datetime,
    ) -> None:
        """Append one finished run: ActorWoke, then (if it succeeded) its findings and
        ActorSuspended. A failed run leaves only ActorWoke, as before."""
        agent = p.registered.agent
        trigger, plan = p.trigger, p.plan
        log = self._log.bind(
            agent_id=agent.agent_id,
            agent_version=agent.version,
            trigger_seq=trigger.seq,
            cut_id=p.cut_id,
        )
        self._append(
            agent.agent_id,
            Visibility.BLINDED,
            ActorWoke(
                agent_id=agent.agent_id,
                agent_version=agent.version,
                trigger_seq=trigger.seq,
                executor=plan.executor,
                review_from_seq=None if review is None else review[0],
                review_to_seq=None if review is None else review[1],
                substrate=plan.substrate,
                started_at=started_at,
            ),
        )
        try:
            if isinstance(outcome, Exception):
                raise outcome
            drafts = list(outcome.drafts)
            prefix = dedup_prefix(agent.agent_id, agent.version)
            for draft in drafts:
                if not draft.dedup_key.startswith(prefix):
                    raise ValueError(
                        f"dedup key {draft.dedup_key!r} does not start with {prefix!r}"
                    )
                # Checked before anything is recorded: no run is half written.
                extra = set(draft.visible_to or ()) - agent.visible_to
                if extra:
                    raise ValueError(
                        f"draft {draft.dedup_key!r} names roles outside "
                        f"{agent.agent_id}'s visible_to: {sorted(extra)}"
                    )
        except Exception as exc:  # an agent failure must never stop the pass
            log.error("agent.run_failed", error=f"{type(exc).__name__}: {exc}")
            report.failed.append((agent.agent_id, trigger.seq, f"{type(exc).__name__}: {exc}"))
            return

        for draft in drafts:
            self._record(p.registered, draft, report)
        self._append(
            agent.agent_id,
            Visibility.BLINDED,
            ActorSuspended(
                agent_id=agent.agent_id,
                agent_version=agent.version,
                trigger_seq=trigger.seq,
                executor=plan.executor,
                image_digest=outcome.image_digest,
                policy_sha256=outcome.policy_sha256,
                inputs_manifest_sha256=outcome.inputs_manifest_sha256,
                policy_file=outcome.policy_file,
                policy_file_sha256=outcome.policy_file_sha256,
                substrate=outcome.substrate,
            ),
        )
        report.completed.append((agent.agent_id, trigger.seq))
        log.info("agent.completed", drafts=len(drafts))

    def _record(self, registered: RegisteredAgent, draft: FindingDraft, report: RunReport) -> None:
        agent = registered.agent
        if self._ledger.find_by_dedup_key(draft.dedup_key) is not None:
            report.findings_skipped.append(draft.dedup_key)
            return
        visible_to = agent.visible_to if draft.visible_to is None else frozenset(draft.visible_to)
        finding = Finding(
            kind=draft.kind,
            agent_id=agent.agent_id,
            agent_version=agent.version,
            study_id=self._study_id,
            site_id=draft.site_id,
            severity=draft.severity,
            title=draft.title,
            summary=draft.summary,
            evidence=draft.evidence,
            visibility=agent.visibility,
            visible_to=visible_to,
            score=draft.score,
            actor_under_review=draft.actor_under_review,
        )
        try:
            self._append(
                agent.agent_id,
                agent.visibility,
                FindingRaised(finding=finding, dedup_key=draft.dedup_key),
            )
        except DuplicateDedupKeyError:
            report.findings_skipped.append(draft.dedup_key)
            return
        report.findings_appended.append(draft.dedup_key)
