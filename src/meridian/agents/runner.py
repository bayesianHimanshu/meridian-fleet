"""One runner pass: wake each enabled agent once per DataCut and record what it finds.

A pass runs independent (agent, DataCut) runs concurrently, as many at a time per executor
as its capacity() allows (for substrate, the pool's free workers); the rest wait in that
executor's queue. The ledger stays single writer: runs never append, the host does, in
one order fixed before anything runs (trigger seq, then agent id). Each run's events
(ActorWoke, its findings, ActorSuspended) are appended together once it and every run
before it in that order have finished, so a pass gives the same ledger content however
long each run takes. ActorWoke is therefore appended after its run, not before, and
carries the run's real start in started_at.

A run whose worker is lost (a Spot preemption) is run once more on a new actor; both
attempts are recorded, the lost one as an ActorWoke with worker_lost and where it ran.

A rerun (--rerun) of a pair the ledger already holds is recorded only when it produced
something new (phase 4b step 3): a failure, a lost worker, or a result whose provenance
(run_provenance: executor, image, policy, inputs manifest, drafts, template, golden
snapshot, dispatcher image) no earlier ActorSuspended of the same agent version and
trigger has. An identical rerun appends nothing; the report counts it as unchanged.
"""

import hashlib
import json
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
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


class RetryableRunError(Exception):
    """A run failed in a way that leaves nothing behind and may be retried once on a new
    sandbox: its worker was lost (a Spot preemption, a node gone). Executors raise a
    subclass (meridian.sandbox.substrate_executor.WorkerLostError)."""


@dataclass(slots=True)
class RunReport:
    completed: list[tuple[str, int]] = field(default_factory=list)
    failed: list[tuple[str, int, str]] = field(default_factory=list)
    findings_appended: list[str] = field(default_factory=list)
    findings_skipped: list[str] = field(default_factory=list)
    executors: dict[str, int] = field(default_factory=dict)  # runs sent to each executor
    capacity: dict[str, int] = field(default_factory=dict)  # runs at a time, per executor
    # Attempts whose worker was lost and that were run again: (agent, trigger seq, error).
    retried: list[tuple[str, int, str]] = field(default_factory=list)
    # Reruns identical to a recorded run, so not recorded again: (agent, trigger seq).
    unchanged: list[tuple[str, int]] = field(default_factory=list)


Provenance = tuple[str | None, ...]


def drafts_sha256(drafts: Sequence[FindingDraft]) -> str:
    """sha256 of the drafts as canonical JSON, in the order the agent returned them."""
    text = json.dumps(
        [d.model_dump(mode="json") for d in drafts], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_provenance(
    executor: str | None,
    image_digest: str | None,
    policy_sha256: str | None,
    policy_file_sha256: str | None,
    inputs_manifest_sha256: str | None,
    drafts: str | None,
    substrate: SubstrateRun | None,
) -> Provenance:
    """What makes a run's result what it is. Not the actor, worker or times: those differ
    on every run and are recorded by var/actors.jsonl."""
    s = substrate
    return (
        executor,
        image_digest,
        policy_sha256,
        policy_file_sha256,
        inputs_manifest_sha256,
        drafts,
        None if s is None else s.version,
        None if s is None else s.template_sha256,
        None if s is None else s.template_file_sha256,
        None if s is None else s.image,
        None if s is None else s.golden_snapshot,
        None if s is None else s.dispatcher_image,
    )


@dataclass(frozen=True, slots=True)
class _Planned:
    registered: RegisteredAgent
    trigger: Event
    cut_id: str
    plan: RunPlan


@dataclass(frozen=True, slots=True)
class _Attempt:
    planned: _Planned
    started_at: datetime
    outcome: RunOutcome | Exception


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
        lost_worker_retries: int = 1,
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
        self._retries = lost_worker_retries
        self._recorded_runs: dict[tuple[str, str | None, int], set[Provenance]] | None = None

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

    def _recorded(self) -> dict[tuple[str, str | None, int], set[Provenance]]:
        """The provenance of every recorded completion, per (agent, version, trigger)."""
        out: dict[tuple[str, str | None, int], set[Provenance]] = {}
        for entry in self._ledger.read(event_types=[EventType.ACTOR_SUSPENDED]):
            if isinstance(entry, Event) and isinstance(entry.payload, ActorSuspended):
                p = entry.payload
                out.setdefault((p.agent_id, p.agent_version, p.trigger_seq), set()).add(
                    run_provenance(
                        p.executor,
                        p.image_digest,
                        p.policy_sha256,
                        p.policy_file_sha256,
                        p.inputs_manifest_sha256,
                        p.drafts_sha256,
                        p.substrate,
                    )
                )
        return out

    def _unchanged(self, attempts: Sequence[_Attempt]) -> bool:
        """A rerun with one attempt that succeeded exactly as a recorded run did."""
        if self._recorded_runs is None or len(attempts) != 1:
            return False
        a = attempts[0]
        outcome = a.outcome
        if isinstance(outcome, Exception):
            return False
        agent = a.planned.registered.agent
        known = self._recorded_runs.get((agent.agent_id, agent.version, a.planned.trigger.seq))
        mine = run_provenance(
            a.planned.plan.executor,
            outcome.image_digest,
            outcome.policy_sha256,
            outcome.policy_file_sha256,
            outcome.inputs_manifest_sha256,
            drafts_sha256(outcome.drafts),
            outcome.substrate,
        )
        return known is not None and mine in known

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
        findings are skipped as duplicates, and the run is recorded only if it produced
        something new (the module docstring); report.unchanged lists the others."""
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
        self._recorded_runs = self._recorded() if rerun else None
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
            futures: list[Future[list[_Attempt]]] = []
            for p in planned:
                report.executors[p.plan.executor] = report.executors.get(p.plan.executor, 0) + 1
                futures.append(pools[p.plan.executor].submit(self._timed_run, p))
            for future in futures:
                self._commit_attempts(future.result(), report)
        finally:
            for pool in pools.values():
                pool.shutdown(wait=True)

    def _timed_run(self, p: _Planned) -> list[_Attempt]:
        """Run one planned run; return each attempt with when it started (read as it
        leaves the queue) and its outcome or the exception it raised. ActorWoke is appended
        later, at commit, and carries that start time. A run whose worker was lost
        (RetryableRunError) is run again once, on a new plan (a new actor); both attempts
        are returned, so the ledger records the lost one as a started run that never
        completed."""
        attempts: list[_Attempt] = []
        current = p
        while True:
            started_at = self._clock.now()
            try:
                outcome: RunOutcome | Exception = self._executor.run(
                    current.registered.agent.agent_id, current.trigger, current.cut_id, current.plan
                )
            except Exception as exc:  # an agent failure must never stop the pass
                outcome = exc
            attempts.append(_Attempt(current, started_at, outcome))
            if not isinstance(outcome, RetryableRunError) or len(attempts) > self._retries:
                return attempts
            try:
                plan = self._executor.plan(current.registered.agent.agent_id, current.trigger)
            except Exception:  # no new plan: the lost attempt stands as the failure
                return attempts
            current = replace(current, plan=plan)

    def _commit_attempts(self, attempts: Sequence[_Attempt], report: RunReport) -> None:
        """Commit a run's attempts in order; a lost attempt that was retried is reported
        as retried, not failed. A rerun identical to a recorded run appends nothing."""
        if self._unchanged(attempts):
            a = attempts[0]
            report.unchanged.append((a.planned.registered.agent.agent_id, a.planned.trigger.seq))
            return
        for i, a in enumerate(attempts):
            retried = i < len(attempts) - 1
            self._commit(
                a.planned,
                a.outcome,
                report,
                started_at=a.started_at,
                retried=retried,
                attempt=i + 1,
            )

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
        attempts = self._timed_run(p)
        for i, a in enumerate(attempts):
            retried = i < len(attempts) - 1
            self._commit(
                a.planned,
                a.outcome,
                report,
                review,
                started_at=a.started_at,
                retried=retried,
                attempt=i + 1,
            )

    def _commit(
        self,
        p: _Planned,
        outcome: RunOutcome | Exception,
        report: RunReport,
        review: tuple[int, int] | None = None,
        *,
        started_at: datetime,
        retried: bool = False,
        attempt: int = 1,
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
        lost = outcome if isinstance(outcome, RetryableRunError) else None
        where = plan.substrate
        if lost is not None and where is not None:
            where = where.model_copy(
                update={
                    "worker_pod": getattr(lost, "worker_pod", None),
                    "worker_node": getattr(lost, "node", None),
                }
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
                substrate=where,
                started_at=started_at,
                attempt=attempt if attempt > 1 else None,
                worker_lost=None if lost is None else f"{type(lost).__name__}: {lost}"[:1000],
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
            error = f"{type(exc).__name__}: {exc}"
            if retried:
                log.warning("agent.run_retried", error=error)
                report.retried.append((agent.agent_id, trigger.seq, error))
            else:
                log.error("agent.run_failed", error=error)
                report.failed.append((agent.agent_id, trigger.seq, error))
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
                drafts_sha256=drafts_sha256(drafts),
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
