"""The term adjudicator's pass: judge pending candidates and record each outcome.

One pass finds every candidate (meridian.agents.safety.term_adjudicator.candidates) among
the FindingRaised events of the adjudicated detector versions that has no final
assessment from this agent version yet (accepted or rejected; an error is retried on the
next pass), oldest cut first. The host reads the four terms, checks the input, and builds
one request per adjudicator model for the first max_calls_per_pass candidates. Each model
gets its own jobs (at most max_per_job requests, one safety.local_model sandbox each), all
of one model before the next, so Ollama loads each model once. Every answer is validated
here, the answers are combined by the agreement rule, and one FindingAssessment with both
answers is recorded per candidate, with the findings' visibility. No finding is changed, closed or
superseded: this module never writes anything but FindingAssessment events.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

import polars as pl
import structlog

from meridian.agents.local_model import LocalBatch, LocalModelExecutor
from meridian.agents.safety import term_adjudicator as ta
from meridian.data.datacut import Ledger
from meridian.data.study_store import StudyReader
from meridian.domain import safety as s
from meridian.domain.enums import EventType, Visibility
from meridian.domain.events import (
    MAX_ANSWER_TEXT,
    AssessmentStatus,
    Event,
    EventDraft,
    FindingAssessment,
    FindingRaised,
    ModelAnswer,
)
from meridian.llm.local import LocalModelRequest
from meridian.llm.local_job import LocalCallResult, LocalModelJob
from meridian.platform.clock import Clock
from meridian.platform.config import LocalModelSettings

FINAL = frozenset({"accepted", "rejected"})


@dataclass(slots=True)
class AdjudicatorReport:
    counts: dict[str, int] = field(default_factory=dict)
    deferred: list[tuple[str, ...]] = field(default_factory=list)  # over budget
    skipped: list[tuple[tuple[str, ...], str]] = field(default_factory=list)  # never sent
    events: list[Event] = field(default_factory=list)
    batch_error: str | None = None

    def add(self, event: Event) -> None:
        assert isinstance(event.payload, FindingAssessment)
        status = event.payload.status
        self.counts[status] = self.counts.get(status, 0) + 1
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class _Pending:
    candidate: ta.Candidate
    inp: dict[str, Any]
    requests: tuple[LocalModelRequest, ...]  # one per adjudicator model, in that order


def raised_events(ledger: Ledger) -> list[Event]:
    return [
        e
        for e in ledger.read(event_types=[EventType.FINDING_RAISED])
        if isinstance(e, Event) and isinstance(e.payload, FindingRaised)
    ]


class AdjudicatorRunner:
    def __init__(
        self,
        *,
        ledger: Ledger,
        executor: LocalModelExecutor,
        clock: Clock,
        settings: LocalModelSettings,
        lookup: ta.TermLookup,
        window_days: int,
        log: structlog.typing.FilteringBoundLogger,
    ) -> None:
        self._ledger = ledger
        self._executor = executor
        self._clock = clock
        self._settings = settings
        self._lookup = lookup
        self._window = window_days
        self._log = log.bind(agent_id=ta.AGENT_ID, agent_version=ta.VERSION)

    def _done(self) -> set[tuple[str, ...]]:
        done = set()
        for entry in self._ledger.read(event_types=[EventType.FINDING_ASSESSMENT]):
            if isinstance(entry, Event) and isinstance(entry.payload, FindingAssessment):
                p = entry.payload
                if (p.agent_id, p.agent_version) == (ta.AGENT_ID, ta.VERSION) and p.status in FINAL:
                    done.add(p.finding_ids)
        return done

    def pending(self, cuts: Collection[str] | None = None) -> list[ta.Candidate]:
        done = self._done()
        return [
            c
            for c in ta.candidates(raised_events(self._ledger), self._window)
            if c.finding_ids not in done and (cuts is None or ta.cut_of(c.events[0]) in cuts)
        ]

    def run_pass(self, cuts: Collection[str] | None = None) -> AdjudicatorReport:
        report = AdjudicatorReport()
        queue: list[_Pending] = []
        for candidate in self.pending(cuts):
            clinical = self._lookup.clinical(candidate.clinical)
            case = self._lookup.case(candidate.case)
            if clinical is None or case is None:
                report.skipped.append((candidate.finding_ids, "record_not_found"))
                self._log.warning("adjudicator.record_not_found", findings=candidate.finding_ids)
                continue
            inp = ta.adjudicator_input(clinical, case)
            problems = ta.input_problems(inp)
            if problems:  # never sent: the input may hold only the four terms
                report.skipped.append((candidate.finding_ids, ",".join(problems)))
                self._log.warning("adjudicator.input_refused", findings=candidate.finding_ids)
                continue
            if len(queue) >= self._settings.max_calls_per_pass:
                report.deferred.append(candidate.finding_ids)
                continue
            requests = tuple(
                ta.build_request(
                    inp,
                    model=model,
                    seed=self._settings.seed,
                    num_ctx=self._settings.num_ctx,
                    num_predict=self._settings.num_predict,
                    temperature=self._settings.temperature,
                    think=self._settings.think,
                )
                for model in self._settings.adjudicator_models
            )
            queue.append(_Pending(candidate, inp, requests))
        if queue:
            self._send(queue, report)
        self._executor.drain()
        self._log.info(
            "adjudicator.pass_done",
            counts=report.counts,
            deferred=len(report.deferred),
            skipped=len(report.skipped),
        )
        return report

    def _record(self, candidate: ta.Candidate, fields: dict[str, Any]) -> Event:
        restricted = any(e.visibility is Visibility.RESTRICTED for e in candidate.events)
        payload = FindingAssessment.model_validate(
            {
                "finding_ids": candidate.finding_ids,
                "finding_seqs": tuple(e.seq for e in candidate.events),
                "candidate": candidate.kind,
                "agent_id": ta.AGENT_ID,
                "agent_version": ta.VERSION,
                "model": "+".join(self._settings.adjudicator_models),
                **fields,
            }
        )
        return self._ledger.append(
            EventDraft(
                occurred_at=self._clock.now(),
                actor=ta.AGENT_ID,
                study_id=candidate.events[0].study_id,
                visibility=Visibility.RESTRICTED if restricted else Visibility.BLINDED,
                payload=payload,
            )
        )

    def _send(self, queue: list[_Pending], report: AdjudicatorReport) -> None:
        """Every model's jobs, one model after the other, then one assessment per candidate."""
        answers: list[list[ModelAnswer]] = [[] for _ in queue]
        common: dict[str, Any] = {"executor": self._executor.name}
        for i, model in enumerate(self._settings.adjudicator_models):
            for start in range(0, len(queue), self._settings.max_per_job):
                chunk = queue[start : start + self._settings.max_per_job]
                requests = [p.requests[i] for p in chunk]
                got, batch = self._send_job(model, requests, report)
                for offset, answer in enumerate(got):
                    answers[start + offset].append(answer)
                if batch is not None:
                    common = {
                        "executor": batch.executor,
                        "sandbox": batch.sandbox,
                        "policy_sha256": batch.policy_sha256,
                    }
        for p, these in zip(queue, answers, strict=True):
            c = ta.combine(these)
            fields = {
                "status": c.status,
                "verdict": c.verdict,
                "reason": c.reason,
                "rejection_reasons": c.rejection_reasons,
                "error": c.error,
                "answers": tuple(these),
                "latency_ms": sum(a.latency_ms or 0 for a in these),
                **common,
            }
            report.add(self._record(p.candidate, fields))

    def _send_job(
        self, model: str, requests: list[LocalModelRequest], report: AdjudicatorReport
    ) -> tuple[list[ModelAnswer], LocalBatch | None]:
        job = LocalModelJob(
            model=model, pinned_digest=self._settings.pinned(model), requests=tuple(requests)
        )
        try:
            batch = self._executor.call(job, self._settings.timeout_seconds)
            if len(batch.results) != len(requests):
                raise RuntimeError(f"{len(batch.results)} results for {len(requests)} requests")
        except Exception as exc:  # a failed batch is an error answer for each request in it
            message = f"{type(exc).__name__}: {exc}"[:500]
            report.batch_error = message
            self._log.error("adjudicator.batch_failed", model=model, error=message)
            return [
                ModelAnswer(model=model, status="error", error=message, request_sha256=r.sha256())
                for r in requests
            ], None
        return [
            self.answer(model, r, result, batch.served_digest)
            for r, result in zip(requests, batch.results, strict=True)
        ], batch

    def answer(
        self,
        model: str,
        request: LocalModelRequest,
        result: LocalCallResult,
        served: str | None,
    ) -> ModelAnswer:
        """One model's answer, validated here; the sandbox only carried the call."""
        sent = request.sha256()
        base: dict[str, Any] = {"model": model, "request_sha256": sent}
        if result.request_sha256 != sent:
            error = "the sandbox sent a different request than the host built"
            return ModelAnswer(status="error", error=error, **base)
        if served != self._settings.pinned(model):
            return ModelAnswer(status="error", error=f"digest_mismatch: served {served}", **base)
        if result.response is None:
            error = f"{result.error_kind}: {result.error}"
            return ModelAnswer(status="error", error=error, model_digest=served, **base)
        r = result.response
        judgement = ta.validate(r)
        status: AssessmentStatus = "accepted" if judgement.accepted else "rejected"
        return ModelAnswer(
            status=status,
            model_digest=served,
            verdict=judgement.verdict,
            reason=judgement.reason,
            text=r.text[:MAX_ANSWER_TEXT],
            rejection_reasons=judgement.reasons,
            response_sha256=r.response_sha256,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            latency_ms=r.latency_ms,
            **base,
        )


def reader_lookup(
    study_reader: Callable[[str], StudyReader], safety_reader: Callable[[str], StudyReader]
) -> ta.TermLookup:
    """Terms from the cut's blinded AE table and its safety database snapshot."""

    def _text(value: object) -> str:
        return "" if value is None else str(value).strip()

    def clinical(ref: ta.ClinicalRef) -> ta.Term | None:
        ae = study_reader(ref.cut_id).table("ae")
        rows = (
            ae.filter((pl.col("USUBJID") == ref.usubjid) & (pl.col("AESEQ") == ref.aeseq))
            .sort(["USUBJID", "AESEQ"], maintain_order=True)
            .iter_rows(named=True)
        )
        row = next(rows, None)
        return None if row is None else ta.Term(_text(row["AEDECOD"]), _text(row["AETERM"]))

    def case(ref: ta.CaseRef) -> ta.Term | None:
        table = safety_reader(ref.cut_id).table(s.SAFETY_CASES)
        rows = table.filter(pl.col(s.CASEID) == ref.caseid)
        if ref.version is not None and s.CASE_VERSION in rows.columns:
            rows = rows.filter(pl.col(s.CASE_VERSION).cast(pl.Int64, strict=False) == ref.version)
        found = rows.sort(rows.columns, maintain_order=True).to_dicts()
        if not found:
            return None
        row = found[-1]
        return ta.Term(_text(row[s.PREFERRED_TERM]), _text(row[s.REPORTED_TERM]))

    return ta.TermLookup(clinical, case)
