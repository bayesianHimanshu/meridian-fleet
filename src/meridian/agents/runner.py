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
from typing import Any, Protocol

import structlog

from meridian.agents.base import FindingDraft
from meridian.agents.registry import AgentRegistry, RegisteredAgent
from meridian.data.datacut import Ledger, data_cut_events, send_study_events
from meridian.domain.audit import LEDGER_SCOPE
from meridian.domain.dedup import dedup_prefix
from meridian.domain.enums import DecisionAction, EventType, FindingKind, Role, Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    AuditReviewRequested,
    DataCut,
    Event,
    EventDraft,
    EventPayload,
    FetchedInput,
    FindingRaised,
    GatePack,
    GateRequest,
    HumanDecision,
    InventoryLoaded,
    LedgerEntry,
    NoaelDecision,
    PortfolioOutlook,
    PortfolioReviewRequested,
    SendStudyLoaded,
    SignalScanRequested,
    SubstrateRun,
    portfolio_study,
)
from meridian.domain.findings import Finding
from meridian.ledger.store import DuplicateDedupKeyError
from meridian.platform.clock import Clock
from meridian.portfolio.reports import REPORT_KINDS
from meridian.portfolio.reports import payload as report_payload


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


# Requests answered once, by whichever version of the agent ran first (wrap-up): a new
# version does not answer an old signal scan again (live openFDA calls), nor an old
# portfolio review or a gate request that may already be decided. --rerun leaves them out.
ONE_SHOT = frozenset(
    {EventType.SIGNAL_SCAN_REQUESTED, EventType.PORTFOLIO_REVIEW_REQUESTED, EventType.GATE_REQUEST}
)
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
        prepare_noael: Callable[[Event, str, Sequence[Event]], object] | None = None,
        keep_fetched: Callable[[Event, Sequence[Any]], object] | None = None,
        prepare_portfolio: Callable[[Event], object] | None = None,
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
        # Phase 5 step 2: writes a NOAEL decision run's extract of the ledger (the study's
        # decisions up to the trigger) for its SEND cut, before the run is planned.
        self._prepare_noael = prepare_noael
        self._decision_study: dict[int, str] = {}  # acceptance seq -> its proposal's study
        # Phase 5 step 3a: keeps a run's fetched responses as its inputs (var/signals).
        self._keep_fetched = keep_fetched
        # Phase 5 step 3b: writes a portfolio review's or gate request's facts, built by
        # the host as the portfolio lead, before the run is planned.
        self._prepare_portfolio = prepare_portfolio
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

    def _study_of(self, trigger: Event) -> str:
        """The study a run belongs to: a SEND study's own id (MRD-101-TOX-01) for the
        preclinical agents (its NOAEL decisions too), the asset (MRD-101) for an inventory,
        else the runner's clinical study."""
        p = trigger.payload
        if isinstance(p, SendStudyLoaded | NoaelDecision | SignalScanRequested):
            return p.study_id
        if isinstance(p, InventoryLoaded):
            return p.asset_id
        if isinstance(p, HumanDecision) and trigger.seq in self._decision_study:
            return self._decision_study[trigger.seq]
        if isinstance(p, PortfolioReviewRequested | GateRequest):
            return portfolio_study(trigger)
        return self._study_id

    def _bridge_triggers(self) -> list[Event]:
        """Phase 5 step 3a (preclinical.translational_bridge): for each SEND study, the
        latest of its latest NoaelDecision and the HumanDecisions that accept one of its dose
        proposals. Listed under HUMAN_DECISION, so only an agent subscribed to that type is
        paired with them; the study of each acceptance is kept for _study_of and _cut_of."""
        proposals: dict[str, str] = {}
        accepts: list[Event] = []
        for e in self._ledger.read(
            event_types=[EventType.FINDING_RAISED, EventType.HUMAN_DECISION]
        ):
            if not isinstance(e, Event):
                continue
            p = e.payload
            if isinstance(p, FindingRaised) and p.finding.kind is FindingKind.DOSE_PROPOSAL:
                if p.finding.finding_id is not None:
                    proposals[p.finding.finding_id] = p.finding.study_id
            elif (
                isinstance(p, HumanDecision)
                and p.decision.action is DecisionAction.ACCEPTED
                and p.decision.finding_id in proposals
            ):
                self._decision_study[e.seq] = proposals[p.decision.finding_id]
                accepts.append(e)
        latest: dict[str, Event] = {}
        for e in [*self._noael_triggers(), *accepts]:
            study = self._study_of(e)
            if study not in latest or e.seq > latest[study].seq:
                latest[study] = e
        return sorted(latest.values(), key=lambda e: e.seq)

    def _noael_triggers(self) -> list[Event]:
        """The latest NoaelDecision of each study: a run on it sees every decision before
        it, so an earlier, already revised decision is never run on its own."""
        latest: dict[str, Event] = {}
        for e in self._ledger.read(event_types=[EventType.NOAEL_DECISION]):
            if isinstance(e, Event) and isinstance(e.payload, NoaelDecision):
                latest[e.payload.study_id] = e
        return sorted(latest.values(), key=lambda e: e.seq)

    def _inventory_triggers(self) -> list[Event]:
        return [
            e
            for e in self._ledger.read(event_types=[EventType.INVENTORY_LOADED])
            if isinstance(e, Event) and isinstance(e.payload, InventoryLoaded)
        ]

    def _cut_of(self, trigger: Event, send: Sequence[Event]) -> str | None:
        """The cut a run reads: the trigger's own, or for a NOAEL decision the SEND cut its
        study was last loaded as before it (None: no SEND study yet)."""
        p = trigger.payload
        if isinstance(p, DataCut | SendStudyLoaded | InventoryLoaded):
            return p.cut_id
        if isinstance(p, SignalScanRequested):
            return p.scan_id
        if isinstance(p, PortfolioReviewRequested):
            return p.review_id
        if isinstance(p, GateRequest):
            return p.gate_request_id
        if isinstance(p, NoaelDecision | HumanDecision):
            study = self._study_of(trigger)
            loads = [
                e.payload.cut_id
                for e in send
                if isinstance(e.payload, SendStudyLoaded)
                and e.payload.study_id == study
                and e.seq < trigger.seq
            ]
            return loads[-1] if loads else None
        return None

    def _fetched(
        self, trigger: Event, outcome: RunOutcome | Exception
    ) -> tuple[FetchedInput, ...] | None:
        """Phase 5 step 3a: a run that fetched from a public source has its responses kept
        (keep_fetched) before anything is recorded, and each one named on its ActorWoke."""
        records = () if isinstance(outcome, Exception) else getattr(outcome, "fetched", ())
        if not records:
            return None
        if self._keep_fetched is None:
            raise ValueError("a run fetched public data and no store for it is set")
        self._keep_fetched(trigger, records)
        return tuple(
            FetchedInput(
                path=r.path,
                query=r.query,
                status=r.status,
                sha256=r.sha256,
                last_updated=r.last_updated,
                bytes=len(r.body.encode("utf-8")),
            )
            for r in records
        )

    def _append(
        self,
        actor: str,
        visibility: Visibility,
        payload: EventPayload,
        study_id: str | None = None,
    ) -> Event:
        return self._ledger.append(
            EventDraft(
                occurred_at=self._clock.now(),
                actor=actor,
                study_id=self._study_id if study_id is None else study_id,
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
        # DataCut events of the clinical study, SendStudyLoaded events of any SEND study,
        # and (phase 5 step 2) the latest NoaelDecision of each study and every
        # InventoryLoaded; each agent is paired only with the trigger types it subscribes to.
        send = send_study_events(self._ledger)
        by_type = {
            EventType.DATA_CUT: data_cut_events(self._ledger, self._study_id),
            EventType.SEND_STUDY_LOADED: send,
            EventType.NOAEL_DECISION: self._noael_triggers(),
            EventType.INVENTORY_LOADED: self._inventory_triggers(),
            # Phase 5 step 3a: the bridge's triggers (NoaelDecisions and acceptances), and
            # every signal scan.
            EventType.HUMAN_DECISION: self._bridge_triggers(),
            EventType.SIGNAL_SCAN_REQUESTED: [
                e
                for e in self._ledger.read(event_types=[EventType.SIGNAL_SCAN_REQUESTED])
                if isinstance(e, Event) and isinstance(e.payload, SignalScanRequested)
            ],
            # Phase 5 step 3b: every portfolio review and gate request.
            EventType.PORTFOLIO_REVIEW_REQUESTED: [
                e
                for e in self._ledger.read(event_types=[EventType.PORTFOLIO_REVIEW_REQUESTED])
                if isinstance(e, Event)
            ],
            EventType.GATE_REQUEST: [
                e
                for e in self._ledger.read(event_types=[EventType.GATE_REQUEST])
                if isinstance(e, Event)
            ],
        }
        cut_of = {t.seq: self._cut_of(t, send) for events in by_type.values() for t in events}
        triggers = {
            kind: [t for t in events if cuts is None or cut_of[t.seq] in cuts]
            for kind, events in by_type.items()
        }
        done = self._completed()
        answered = {(agent_id, seq) for agent_id, _version, seq in done}
        pending = [
            (registered, trigger)
            for registered in self._registry.enabled()
            if agents is None or registered.agent.agent_id in agents
            for kind in by_type
            if kind in registered.agent.subscribes_to
            for trigger in triggers[kind]
            if (
                (registered.agent.agent_id, trigger.seq) not in answered
                if kind in ONE_SHOT
                else rerun
                or (registered.agent.agent_id, registered.agent.version, trigger.seq) not in done
            )
        ]
        pending.sort(key=lambda pair: (pair[1].seq, pair[0].agent.agent_id))
        self._require_sandbox(pending)
        self._recorded_runs = self._recorded() if rerun else None
        planned: list[_Planned] = []
        for registered, trigger in pending:
            cut = cut_of[trigger.seq]
            if cut is None:
                why = f"no SEND study loaded before NOAEL decision seq {trigger.seq}"
                report.failed.append((registered.agent.agent_id, trigger.seq, why))
                continue
            if isinstance(trigger.payload, NoaelDecision | HumanDecision):
                if self._prepare_noael is None:
                    report.failed.append(
                        (registered.agent.agent_id, trigger.seq, "no NOAEL extract writer")
                    )
                    continue
                # The decisions, and (step 3a) the findings and decisions the bridge's
                # tables are built from; the host writes the extract, agents never read it.
                events = [
                    e
                    for e in self._ledger.read(
                        event_types=[
                            EventType.NOAEL_DECISION,
                            EventType.FINDING_RAISED,
                            EventType.HUMAN_DECISION,
                        ]
                    )
                    if isinstance(e, Event)
                ]
                self._prepare_noael(trigger, cut, events)
            if isinstance(trigger.payload, PortfolioReviewRequested | GateRequest):
                if self._prepare_portfolio is None:
                    report.failed.append(
                        (registered.agent.agent_id, trigger.seq, "no portfolio facts writer")
                    )
                    continue
                self._prepare_portfolio(trigger)
            plan = self._plan(registered, trigger, report)
            if plan is not None:
                planned.append(_Planned(registered, trigger, cut, plan))
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
        # Phase 5 step 2: a NOAEL decision is operational when its SEND cut is, and an
        # inventory load always is (INVENTORY_CUT_PREFIX).
        send = send_study_events(self._ledger)

        def operational(t: Event) -> str | None:
            p = t.payload
            if isinstance(p, DataCut) and p.cut_id in self._operational:
                return p.cut_id
            if isinstance(p, InventoryLoaded | SignalScanRequested):
                return self._cut_of(t, send)
            if isinstance(p, PortfolioReviewRequested | GateRequest):
                return self._cut_of(t, send)  # always operational (phase 5 step 3b)
            if isinstance(p, NoaelDecision | HumanDecision):
                cut = self._cut_of(t, send)
                return cut if cut in self._operational else None
            return None

        refused = sorted(
            {
                (cut, r.agent.agent_id, executor)
                for r, t in pending
                if (cut := operational(t)) is not None
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
        study = self._study_of(trigger)
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
                fetched=self._fetched(trigger, outcome),
            ),
            study,
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
            self._record(p.registered, draft, report, study, trigger.seq)
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
            study,
        )
        report.completed.append((agent.agent_id, trigger.seq))
        log.info("agent.completed", drafts=len(drafts))

    def _record_report(
        self,
        registered: RegisteredAgent,
        draft: FindingDraft,
        report: RunReport,
        study_id: str | None,
        trigger_seq: int,
    ) -> None:
        """A report draft (phase 5 step 3b) becomes its event, PortfolioOutlook or GatePack,
        never a finding. Once per dedup key, as a finding is."""
        agent = registered.agent
        if draft.dedup_key in self._reported():
            report.findings_skipped.append(draft.dedup_key)
            return
        self._append(
            agent.agent_id,
            agent.visibility,
            report_payload(draft, agent.agent_id, agent.version, trigger_seq),
            study_id,
        )
        report.findings_appended.append(draft.dedup_key)

    def _reported(self) -> set[str]:
        return {
            e.payload.dedup_key
            for e in self._ledger.read(
                event_types=[EventType.PORTFOLIO_OUTLOOK, EventType.GATE_PACK]
            )
            if isinstance(e, Event) and isinstance(e.payload, PortfolioOutlook | GatePack)
        }

    def _record(
        self,
        registered: RegisteredAgent,
        draft: FindingDraft,
        report: RunReport,
        study_id: str | None = None,
        trigger_seq: int | None = None,
    ) -> None:
        agent = registered.agent
        if draft.kind in REPORT_KINDS:
            if trigger_seq is None:
                raise ValueError("a report draft needs its trigger")
            self._record_report(registered, draft, report, study_id, trigger_seq)
            return
        if self._ledger.find_by_dedup_key(draft.dedup_key) is not None:
            report.findings_skipped.append(draft.dedup_key)
            return
        visible_to = agent.visible_to if draft.visible_to is None else frozenset(draft.visible_to)
        finding = Finding(
            kind=draft.kind,
            agent_id=agent.agent_id,
            agent_version=agent.version,
            study_id=self._study_id if study_id is None else study_id,
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
                finding.study_id,
            )
        except DuplicateDedupKeyError:
            report.findings_skipped.append(draft.dedup_key)
            return
        report.findings_appended.append(draft.dedup_key)
