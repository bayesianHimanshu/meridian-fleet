"""One runner pass: wake each enabled agent once per DataCut and record what it finds."""

from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.registry import AgentRegistry, RegisteredAgent
from meridian.data.datacut import Ledger, data_cut_events
from meridian.data.study_store import StudyReader
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

ReaderFactory = Callable[[str], StudyReader]


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
        reader_factory: ReaderFactory,
        clock: Clock,
        study_id: str,
        log: structlog.typing.FilteringBoundLogger,
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._reader_factory = reader_factory
        self._clock = clock
        self._study_id = study_id
        self._log = log

    def _completed_pairs(self) -> set[tuple[str, int]]:
        done: set[tuple[str, int]] = set()
        for entry in self._ledger.read(event_types=[EventType.ACTOR_SUSPENDED]):
            if isinstance(entry, Event) and isinstance(entry.payload, ActorSuspended):
                done.add((entry.payload.agent_id, entry.payload.trigger_seq))
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
        done = self._completed_pairs()
        for registered in self._registry.enabled():
            agent = registered.agent
            if EventType.DATA_CUT not in agent.subscribes_to:
                continue
            for trigger in triggers:
                if (agent.agent_id, trigger.seq) in done:
                    continue
                self._run_one(registered, trigger, report)
        return report

    def _run_one(self, registered: RegisteredAgent, trigger: Event, report: RunReport) -> None:
        agent = registered.agent
        assert isinstance(trigger.payload, DataCut)
        cut_id = trigger.payload.cut_id
        log = self._log.bind(agent_id=agent.agent_id, trigger_seq=trigger.seq, cut_id=cut_id)
        self._append(
            agent.agent_id,
            Visibility.BLINDED,
            ActorWoke(agent_id=agent.agent_id, trigger_seq=trigger.seq),
        )
        try:
            ctx = AgentContext(
                study=self._reader_factory(cut_id),
                clock=self._clock,
                config=registered.config,
                log=log,
            )
            drafts = list(agent.run(trigger, ctx))
            for draft in drafts:
                if not draft.dedup_key.startswith(f"{agent.agent_id}|"):
                    raise ValueError(f"dedup key {draft.dedup_key!r} does not start with agent id")
        except Exception as exc:  # an agent failure must never stop the pass
            log.error("agent.run_failed", error=f"{type(exc).__name__}: {exc}")
            report.failed.append((agent.agent_id, trigger.seq, f"{type(exc).__name__}: {exc}"))
            return

        for draft in drafts:
            self._record(registered, draft, report)
        self._append(
            agent.agent_id,
            Visibility.BLINDED,
            ActorSuspended(agent_id=agent.agent_id, trigger_seq=trigger.seq),
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
