"""The local narrator's pass: explain pending safety findings with the local model.

One pass reads every FindingRaised of the detector versions safety.local_narrator
narrates that has no final FindingNarrative from it yet (accepted or rejected; an error
is retried), oldest first, sends the first max_calls_per_pass as one job to the
safety.local_model sandbox, validates every answer here and records a FindingNarrative
with the finding's visibility, the model and the digest it was served with. The finding
itself is never changed.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

import structlog

from meridian.agents.local_model import LocalModelExecutor
from meridian.agents.safety import local_narrator as ln
from meridian.data.datacut import Ledger
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType
from meridian.domain.events import Event, EventDraft, FindingNarrative, FindingRaised
from meridian.llm.local import LocalModelRequest
from meridian.llm.local_job import LocalCallResult, LocalModelJob
from meridian.platform.clock import Clock
from meridian.platform.config import LocalModelSettings

FINAL = frozenset({"accepted", "rejected"})


@dataclass(slots=True)
class LocalNarratorReport:
    counts: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)  # rejection reason codes
    deferred: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    batch_error: str | None = None

    def add(self, event: Event) -> None:
        assert isinstance(event.payload, FindingNarrative)
        p = event.payload
        self.counts[p.status] = self.counts.get(p.status, 0) + 1
        for code in dict.fromkeys(r.split(":", 1)[0] for r in p.rejection_reasons):
            self.reasons[code] = self.reasons.get(code, 0) + 1
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class _Pending:
    event: Event
    inp: dict[str, Any]
    request: LocalModelRequest


class LocalNarratorRunner:
    def __init__(
        self,
        *,
        ledger: Ledger,
        executor: LocalModelExecutor,
        clock: Clock,
        settings: LocalModelSettings,
        log: structlog.typing.FilteringBoundLogger,
    ) -> None:
        self._ledger = ledger
        self._executor = executor
        self._clock = clock
        self._settings = settings
        self._log = log.bind(agent_id=ln.AGENT_ID, agent_version=ln.VERSION)

    def _done(self) -> set[str]:
        done = set()
        for entry in self._ledger.read(event_types=[EventType.FINDING_NARRATIVE]):
            if isinstance(entry, Event) and isinstance(entry.payload, FindingNarrative):
                p = entry.payload
                if (p.agent_id, p.agent_version) == (ln.AGENT_ID, ln.VERSION) and p.status in FINAL:
                    done.add(p.finding_id)
        return done

    def pending(self, cuts: Collection[str] | None = None) -> list[Event]:
        done = self._done()
        out = []
        for entry in self._ledger.read(event_types=[EventType.FINDING_RAISED]):
            if not isinstance(entry, Event) or not isinstance(entry.payload, FindingRaised):
                continue
            finding = entry.payload.finding
            if not ln.may_narrate(finding) or finding.finding_id in done:
                continue
            if cuts is not None and parse_dedup_key(entry.payload.dedup_key).cut_id not in cuts:
                continue
            out.append(entry)
        return sorted(out, key=lambda e: e.seq)

    def run_pass(self, cuts: Collection[str] | None = None) -> LocalNarratorReport:
        report = LocalNarratorReport()
        queue: list[_Pending] = []
        for event in self.pending(cuts):
            assert isinstance(event.payload, FindingRaised)
            if len(queue) >= self._settings.max_calls_per_pass:
                report.deferred.append(event.payload.finding.finding_id or "")
                continue
            inp = ln.narrator_input(event.payload.finding)
            request = ln.build_request(
                inp,
                model=self._settings.model,
                seed=self._settings.seed,
                num_ctx=self._settings.num_ctx,
                num_predict=self._settings.num_predict,
                temperature=self._settings.temperature,
                think=self._settings.think,
            )
            queue.append(_Pending(event, inp, request))
        if queue:
            self._send(queue, report)
        self._executor.drain()
        self._log.info("local_narrator.pass_done", counts=report.counts, reasons=report.reasons)
        return report

    def _record(self, trigger: Event, fields: dict[str, Any]) -> Event:
        assert isinstance(trigger.payload, FindingRaised)
        finding = trigger.payload.finding
        payload = FindingNarrative.model_validate(
            {
                "finding_id": finding.finding_id,
                "finding_seq": trigger.seq,
                "agent_id": ln.AGENT_ID,
                "agent_version": ln.VERSION,
                "model": self._settings.model,
                **fields,
            }
        )
        return self._ledger.append(
            EventDraft(
                occurred_at=self._clock.now(),
                actor=ln.AGENT_ID,
                study_id=trigger.study_id,
                visibility=trigger.visibility,
                payload=payload,
            )
        )

    def _send(self, queue: list[_Pending], report: LocalNarratorReport) -> None:
        """One job (and one sandbox) per max_per_job requests."""
        size = self._settings.max_per_job
        for start in range(0, len(queue), size):
            self._send_job(queue[start : start + size], report)

    def _send_job(self, queue: list[_Pending], report: LocalNarratorReport) -> None:
        job = LocalModelJob(
            model=self._settings.model,
            pinned_digest=self._settings.pinned(),
            requests=tuple(p.request for p in queue),
        )
        try:
            batch = self._executor.call(job, self._settings.timeout_seconds)
            if len(batch.results) != len(queue):
                raise RuntimeError(f"{len(batch.results)} results for {len(queue)} requests")
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:500]
            report.batch_error = message
            self._log.error("local_narrator.batch_failed", error=message)
            for p in queue:
                fields = {
                    "status": "error",
                    "error": message,
                    "request_sha256": p.request.sha256(),
                    "executor": self._executor.name,
                }
                report.add(self._record(p.event, fields))
            return
        common: dict[str, Any] = {
            "executor": batch.executor,
            "sandbox": batch.sandbox,
            "policy_sha256": batch.policy_sha256,
            "model_digest": batch.served_digest,
        }
        for p, result in zip(queue, batch.results, strict=True):
            served = batch.served_digest
            report.add(self._record(p.event, self.outcome(p, result, served) | common))

    def outcome(self, p: _Pending, result: LocalCallResult, served: str | None) -> dict[str, Any]:
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
        if served != self._settings.pinned():
            return {
                "status": "error",
                "error": f"digest_mismatch: served {served}",
                "request_sha256": sent,
            }
        r = result.response
        v = ln.validate(r, p.inp)
        return {
            "status": "accepted" if v.accepted else "rejected",
            "headline": v.headline,
            "explanation": v.explanation,
            "rejection_reasons": v.reasons,
            "request_sha256": sent,
            "response_sha256": r.response_sha256,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "latency_ms": r.latency_ms,
        }
