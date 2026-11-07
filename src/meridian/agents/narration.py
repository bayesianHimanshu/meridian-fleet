"""The narrator pass: explain pending findings, within a call budget, and record each outcome.

One pass reads every FindingRaised from the three detection agents that has no final
FindingNarrative from this narrator version yet (accepted, rejected or refused; an error
is retried on the next pass), oldest first. Each input passes the de-identification check
first: a refusal is recorded at once and costs no call. The first max_calls of the rest
are sent in one batch by the executor; the others wait for the next pass. Every response
is validated here, on the host, and recorded as a FindingNarrative with the finding's
visibility. The finding itself is never changed.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from meridian.agents.rbqm import narrator
from meridian.data.datacut import Ledger
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType
from meridian.domain.events import (
    Event,
    EventDraft,
    FindingNarrative,
    FindingRaised,
    NarrativeStatus,
)
from meridian.ledger.hashing import canonical_json
from meridian.llm.client import ModelClient, ModelRequest
from meridian.llm.job import CallResult, NarrationJob, run_job
from meridian.platform.clock import Clock
from meridian.platform.config import LlmSettings

INPROCESS = "inprocess"
FINAL = frozenset({"accepted", "rejected", "refused"})


@dataclass(frozen=True, slots=True)
class NarrationBatch:
    results: tuple[CallResult, ...]
    executor: str
    sandbox: str | None = None
    policy_sha256: str | None = None


class NarrationExecutor(Protocol):
    @property
    def name(self) -> str: ...

    def call(self, requests: Sequence[ModelRequest], timeout_seconds: float) -> NarrationBatch:
        """Send every request; one CallResult each, in order. Raise if the batch failed."""
        ...

    def drain(self) -> None: ...


class InProcessNarrationExecutor:
    """Calls the model client in this process. Tests and CI use it with FakeModelClient;
    the real client refuses a real key, so real calls only happen in the sandbox."""

    name = INPROCESS

    def __init__(self, client: ModelClient) -> None:
        self._client = client

    def call(self, requests: Sequence[ModelRequest], timeout_seconds: float) -> NarrationBatch:
        job = NarrationJob(requests=tuple(requests))
        return NarrationBatch(results=run_job(job, self._client).results, executor=self.name)

    def drain(self) -> None:
        """Nothing runs in the background in process."""


@dataclass(slots=True)
class NarratorReport:
    counts: dict[str, int] = field(default_factory=dict)
    deferred: list[str] = field(default_factory=list)  # over budget, for the next pass
    events: list[Event] = field(default_factory=list)
    batch_error: str | None = None

    def add(self, event: Event) -> None:
        assert isinstance(event.payload, FindingNarrative)
        self.counts[event.payload.status] = self.counts.get(event.payload.status, 0) + 1
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class _Pending:
    event: Event
    finding: FindingRaised
    inp: dict[str, Any]
    request: ModelRequest


class NarratorRunner:
    def __init__(
        self,
        *,
        ledger: Ledger,
        executor: NarrationExecutor,
        clock: Clock,
        llm: LlmSettings,
        max_calls: int,
        log: structlog.typing.FilteringBoundLogger,
    ) -> None:
        self._ledger = ledger
        self._executor = executor
        self._clock = clock
        self._llm = llm
        self._max_calls = max_calls
        self._log = log.bind(agent_id=narrator.AGENT_ID, agent_version=narrator.VERSION)

    def _done(self) -> set[str]:
        done = set()
        for entry in self._ledger.read(event_types=[EventType.FINDING_NARRATIVE]):
            if isinstance(entry, Event) and isinstance(entry.payload, FindingNarrative):
                p = entry.payload
                if p.agent_version == narrator.VERSION and p.status in FINAL:
                    done.add(p.finding_id)
        return done

    def pending(self, cuts: Collection[str] | None = None) -> list[Event]:
        """FindingRaised events still to narrate, oldest first; optionally only some cuts."""
        done = self._done()
        out = []
        for entry in self._ledger.read(event_types=[EventType.FINDING_RAISED]):
            if not isinstance(entry, Event) or not isinstance(entry.payload, FindingRaised):
                continue
            finding = entry.payload.finding
            if finding.agent_id not in narrator.SUBSCRIBES_TO_AGENTS:
                continue
            if finding.finding_id is None or finding.finding_id in done:
                continue
            if cuts is not None and parse_dedup_key(entry.payload.dedup_key).cut_id not in cuts:
                continue
            out.append(entry)
        return sorted(out, key=lambda e: e.seq)

    def _record(self, trigger: Event, fields: dict[str, Any]) -> Event:
        assert isinstance(trigger.payload, FindingRaised)
        finding = trigger.payload.finding
        payload = FindingNarrative.model_validate(
            {
                "finding_id": finding.finding_id,
                "finding_seq": trigger.seq,
                "agent_id": narrator.AGENT_ID,
                "agent_version": narrator.VERSION,
                "model": self._llm.model,
                **fields,
            }
        )
        return self._ledger.append(
            EventDraft(
                occurred_at=self._clock.now(),
                actor=narrator.AGENT_ID,
                study_id=trigger.study_id,
                visibility=trigger.visibility,  # the finding's own visibility
                payload=payload,
            )
        )

    def run_pass(self, cuts: Collection[str] | None = None) -> NarratorReport:
        report = NarratorReport()
        queue: list[_Pending] = []
        for event in self.pending(cuts):
            assert isinstance(event.payload, FindingRaised)
            inp = narrator.narrator_input(event.payload.finding)
            problems = narrator.deid_problems(canonical_json(inp))
            if problems:
                report.add(
                    self._record(event, {"status": "refused", "rejection_reasons": problems})
                )
                self._log.warning("narrator.refused", finding_seq=event.seq, reasons=problems)
                continue
            if len(queue) >= self._max_calls:
                assert event.payload.finding.finding_id is not None
                report.deferred.append(event.payload.finding.finding_id)
                continue
            request = narrator.build_request(
                inp,
                model=self._llm.model,
                max_tokens=self._llm.max_tokens,
                temperature=self._llm.temperature,
                disable_thinking=self._llm.disable_thinking,
            )
            queue.append(_Pending(event, event.payload, inp, request))
        if queue:
            self._send(queue, report)
        self._executor.drain()
        self._log.info("narrator.pass_done", counts=report.counts, deferred=len(report.deferred))
        return report

    def _send(self, queue: list[_Pending], report: NarratorReport) -> None:
        try:
            batch = self._executor.call([p.request for p in queue], self._llm.timeout_seconds)
            if len(batch.results) != len(queue):
                raise RuntimeError(f"{len(batch.results)} results for {len(queue)} requests")
        except Exception as exc:  # a failed batch is an error for each finding in it
            message = f"{type(exc).__name__}: {exc}"[:500]
            report.batch_error = message
            self._log.error("narrator.batch_failed", error=message)
            for p in queue:
                report.add(
                    self._record(
                        p.event,
                        {
                            "status": "error",
                            "error": message,
                            "request_sha256": p.request.sha256(),
                            "executor": self._executor.name,
                        },
                    )
                )
            return
        common: dict[str, Any] = {
            "executor": batch.executor,
            "sandbox": batch.sandbox,
            "policy_sha256": batch.policy_sha256,
        }
        for p, result in zip(queue, batch.results, strict=True):
            report.add(self._record(p.event, self._outcome(p, result) | common))

    @staticmethod
    def _outcome(p: _Pending, result: CallResult) -> dict[str, Any]:
        sent = p.request.sha256()
        if result.request_sha256 != sent:
            return {
                "status": "error",
                "error": "the sandbox sent a different request than the host built",
                "request_sha256": sent,
            }
        if result.response is None:
            return {
                "status": "error",
                "error": f"{result.error_kind}: {result.error}",
                "request_sha256": sent,
            }
        r = result.response
        v = narrator.validate(r.text, p.inp, r.stop_reason)
        status: NarrativeStatus = "accepted" if v.accepted else "rejected"
        return {
            "status": status,
            "headline": v.headline,
            "explanation": v.explanation,
            "rejection_reasons": v.reasons,
            "request_sha256": sent,
            "response_sha256": r.response_sha256,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "latency_ms": r.latency_ms,
        }
