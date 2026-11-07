"""One runner pass: wake each enabled agent once per DataCut and record what it finds."""

from dataclasses import dataclass, field
from typing import Protocol

import structlog

from meridian.agents.base import FindingDraft
from meridian.agents.registry import AgentRegistry, RegisteredAgent
from meridian.data.datacut import Ledger, data_cut_events
from meridian.domain.dedup import dedup_prefix
from meridian.domain.enums import EventType, Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    DataCut,
    Event,
    EventDraft,
    EventPayload,
    FindingRaised,
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


class AgentExecutor(Protocol):
    """Where a run happens (meridian.sandbox.executor.Executor)."""

    @property
    def name(self) -> str: ...

    def run(self, agent_id: str, trigger: Event, cut_id: str) -> RunOutcome: ...


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
    ) -> None:
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

    def run_pending(self) -> RunReport:
        report = RunReport()
        triggers = data_cut_events(self._ledger, self._study_id)
        done = self._completed()
        for registered in self._registry.enabled():
            agent = registered.agent
            if EventType.DATA_CUT not in agent.subscribes_to:
                continue
            for trigger in triggers:
                if (agent.agent_id, agent.version, trigger.seq) in done:
                    continue
                self._run_one(registered, trigger, report)
        return report

    def _run_one(self, registered: RegisteredAgent, trigger: Event, report: RunReport) -> None:
        agent = registered.agent
        assert isinstance(trigger.payload, DataCut)
        cut_id = trigger.payload.cut_id
        log = self._log.bind(
            agent_id=agent.agent_id,
            agent_version=agent.version,
            trigger_seq=trigger.seq,
            cut_id=cut_id,
        )
        self._append(
            agent.agent_id,
            Visibility.BLINDED,
            ActorWoke(
                agent_id=agent.agent_id,
                agent_version=agent.version,
                trigger_seq=trigger.seq,
                executor=self._executor.name,
            ),
        )
        try:
            outcome = self._executor.run(agent.agent_id, trigger, cut_id)
            drafts = list(outcome.drafts)
            prefix = dedup_prefix(agent.agent_id, agent.version)
            for draft in drafts:
                if not draft.dedup_key.startswith(prefix):
                    raise ValueError(
                        f"dedup key {draft.dedup_key!r} does not start with {prefix!r}"
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
                executor=self._executor.name,
                image_digest=outcome.image_digest,
                policy_sha256=outcome.policy_sha256,
                inputs_manifest_sha256=outcome.inputs_manifest_sha256,
            ),
        )
        report.completed.append((agent.agent_id, trigger.seq))
        log.info("agent.completed", drafts=len(drafts))

    def _record(self, registered: RegisteredAgent, draft: FindingDraft, report: RunReport) -> None:
        agent = registered.agent
        if self._ledger.find_by_dedup_key(draft.dedup_key) is not None:
            report.findings_skipped.append(draft.dedup_key)
            return
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
            visible_to=agent.visible_to,
            score=draft.score,
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
