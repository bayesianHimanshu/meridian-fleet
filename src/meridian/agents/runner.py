"""One runner pass: wake each enabled agent once per DataCut and record what it finds."""

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
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
        self._require_sandbox(pending)
        for registered, trigger in pending:
            self._run_one(registered, trigger, report)
        self._executor.drain()  # a pass ends only when every sandbox is torn down
        return report

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
        cut_id: str | None = None,
        review: tuple[int, int] | None = None,
    ) -> None:
        agent = registered.agent
        if cut_id is None:
            assert isinstance(trigger.payload, DataCut)
            cut_id = trigger.payload.cut_id
        log = self._log.bind(
            agent_id=agent.agent_id,
            agent_version=agent.version,
            trigger_seq=trigger.seq,
            cut_id=cut_id,
        )
        try:
            plan = self._executor.plan(agent.agent_id, trigger)
        except Exception as exc:  # no template, no golden snapshot: nothing is recorded
            log.error("agent.plan_failed", error=f"{type(exc).__name__}: {exc}")
            report.failed.append((agent.agent_id, trigger.seq, f"{type(exc).__name__}: {exc}"))
            return
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
            ),
        )
        try:
            outcome = self._executor.run(agent.agent_id, trigger, cut_id, plan)
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
            self._record(registered, draft, report)
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
