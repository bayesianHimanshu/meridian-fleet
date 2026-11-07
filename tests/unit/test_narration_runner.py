"""The narrator pass with the fake model client: budget, statuses, retries, records."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
import structlog

from meridian.agents.narration import InProcessNarrationExecutor, NarrationBatch, NarratorRunner
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event, EventDraft, FindingNarrative, FindingRaised
from meridian.domain.findings import Evidence, Finding
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.llm.client import FakeModelClient, ModelClientError, ModelRequest
from meridian.platform.clock import FixedClock
from meridian.platform.config import LlmSettings
from tests.support import T0, make_ledger

LLM = LlmSettings()


def _finding(site: str, agent_id: str = "rbqm.kri_monitor", summary: str = "") -> Finding:
    return Finding(
        kind=FindingKind.KRI_BREACH,
        agent_id=agent_id,
        agent_version="2.1.0",
        study_id="MRD-201",
        site_id=site,
        severity=Severity.HIGH,
        title=f"Site {site}: more protocol deviations than expected",
        summary=summary or f"Site {site} has 18 protocol deviations; the rest implies 5.1.",
        evidence=(
            Evidence(
                source="ops/deviations",
                selector=f"SITEID={site}",
                statistic="deviations:observed",
                value=18.0,
            ),
            Evidence(
                source="ops/deviations",
                selector=f"SITEID={site}",
                statistic="deviations:expected",
                value=5.0945484733880635,
            ),
        ),
        visibility=Visibility.BLINDED,
        visible_to=frozenset({Role.CLINICAL_OPS_LEAD}),
        score=1.0,
    )


def _raise(ledger: SqliteLedger, finding: Finding, cut: str = "DC-BASE") -> Event:
    key = make_dedup_key(
        agent_id=finding.agent_id,
        agent_version=finding.agent_version,
        kind=finding.kind,
        study_id="MRD-201",
        site_id=finding.site_id,
        cut_id=cut,
        check=finding.site_id,
    )
    return ledger.append(
        EventDraft(
            occurred_at=T0,
            actor=finding.agent_id,
            study_id="MRD-201",
            visibility=Visibility.BLINDED,
            payload=FindingRaised(finding=finding, dedup_key=key),
        )
    )


def _good(site: str) -> str:
    return json.dumps(
        {
            "headline": f"Site {site} has more protocol deviations than expected",
            "explanation": f"Site {site} recorded 18 protocol deviations where 5.1 were expected.",
        }
    )


def _runner(ledger: SqliteLedger, client: FakeModelClient, max_calls: int = 10) -> NarratorRunner:
    return NarratorRunner(
        ledger=ledger,
        executor=InProcessNarrationExecutor(client),
        clock=FixedClock(T0),
        llm=LLM,
        max_calls=max_calls,
        log=structlog.get_logger(),
    )


def _narratives(ledger: SqliteLedger) -> list[FindingNarrative]:
    out = []
    for e in ledger.read(event_types=[EventType.FINDING_NARRATIVE]):
        assert isinstance(e, Event)
        assert isinstance(e.payload, FindingNarrative)
        out.append(e.payload)
    return out


@pytest.fixture
def ledger(tmp_path: Path) -> SqliteLedger:
    return make_ledger(tmp_path / "l.sqlite3", FixedClock(T0))


def test_an_accepted_narrative_is_recorded_and_the_finding_is_unchanged(
    ledger: SqliteLedger,
) -> None:
    raised = _raise(ledger, _finding("1016"))
    before = ledger.read()
    report = _runner(ledger, FakeModelClient(script=[_good("1016")])).run_pass()
    assert report.counts == {"accepted": 1}
    (narrative,) = _narratives(ledger)
    assert narrative.status == "accepted"
    assert narrative.finding_id == "RBQM-0001"
    assert narrative.finding_seq == raised.seq
    assert narrative.headline
    assert "1016" in narrative.headline
    assert (narrative.input_tokens, narrative.output_tokens) == (100, 40)
    assert narrative.model == LLM.model
    assert narrative.executor == "inprocess"
    assert report.events[0].visibility is Visibility.BLINDED
    assert report.events[0].actor == "rbqm.narrator"
    assert ledger.read()[: len(before)] == before  # nothing earlier changed
    assert verify(ledger).ok


def test_the_request_hash_is_of_the_canonical_body(ledger: SqliteLedger) -> None:
    _raise(ledger, _finding("1016"))
    client = FakeModelClient(script=[_good("1016")])
    _runner(ledger, client).run_pass()
    (narrative,) = _narratives(ledger)
    (request,) = client.requests
    assert narrative.request_sha256 == request.sha256()
    assert request.max_tokens == 300
    assert request.temperature is None  # claude-haiku-5-5 refuses temperature
    assert "temperature" not in request.body()
    assert request.body()["thinking"] == {"type": "disabled"}


def test_a_rejection_records_reasons_and_no_text(ledger: SqliteLedger) -> None:
    _raise(ledger, _finding("1016"))
    reply = json.dumps({"headline": "Deviations", "explanation": "There were 42 of them."})
    _runner(ledger, FakeModelClient(script=[reply])).run_pass()
    (narrative,) = _narratives(ledger)
    assert narrative.status == "rejected"
    assert narrative.headline is None
    assert narrative.explanation is None
    assert narrative.rejection_reasons == ("missing_site", "number_not_in_evidence:42")
    assert narrative.response_sha256 is not None


def test_a_deid_refusal_sends_nothing_and_costs_no_call(ledger: SqliteLedger) -> None:
    _raise(ledger, _finding("1016", summary="Subject 201-1016-004 had 18 deviations."))
    _raise(ledger, _finding("1017"))
    client = FakeModelClient(script=[_good("1017")])
    report = _runner(ledger, client, max_calls=1).run_pass()
    assert report.counts == {"refused": 1, "accepted": 1}
    refused = _narratives(ledger)[0]
    assert (refused.status, refused.rejection_reasons) == ("refused", ("subject_id_pattern",))
    assert refused.request_sha256 is None
    assert len(client.requests) == 1  # only 1017 was sent
    assert "201-1016-004" not in client.requests[0].user


def test_findings_over_budget_wait_for_the_next_pass(ledger: SqliteLedger) -> None:
    for site in ("1001", "1002", "1003"):
        _raise(ledger, _finding(site))
    first = _runner(ledger, FakeModelClient(script=[_good("1001"), _good("1002")]), 2).run_pass()
    assert first.counts == {"accepted": 2}
    assert first.deferred == ["RBQM-0003"]
    second = _runner(ledger, FakeModelClient(script=[_good("1003")]), 2).run_pass()
    assert second.counts == {"accepted": 1}
    assert second.deferred == []
    third = _runner(ledger, FakeModelClient(script=[]), 2).run_pass()
    assert third.counts == {}
    assert [n.finding_id for n in _narratives(ledger)] == ["RBQM-0001", "RBQM-0002", "RBQM-0003"]


def test_a_transport_failure_is_an_error_and_is_retried_next_pass(ledger: SqliteLedger) -> None:
    _raise(ledger, _finding("1016"))
    down = ModelClientError("transport", "ConnectError after one retry: reset")
    first = _runner(ledger, FakeModelClient(script=[down])).run_pass()
    assert first.counts == {"error": 1}
    (error,) = _narratives(ledger)
    assert error.status == "error"
    assert error.error is not None
    assert error.error.startswith("transport: ConnectError")
    assert error.request_sha256 is not None
    second = _runner(ledger, FakeModelClient(script=[_good("1016")])).run_pass()
    assert second.counts == {"accepted": 1}
    assert [n.status for n in _narratives(ledger)] == ["error", "accepted"]
    assert verify(ledger).ok


class _BrokenExecutor:
    name = "openshell"

    def call(self, requests: Sequence[ModelRequest], timeout_seconds: float) -> NarrationBatch:
        raise RuntimeError("sandbox create failed")

    def drain(self) -> None:
        pass


def test_a_failed_batch_is_an_error_for_each_finding(ledger: SqliteLedger) -> None:
    _raise(ledger, _finding("1001"))
    _raise(ledger, _finding("1002"))
    runner = NarratorRunner(
        ledger=ledger,
        executor=_BrokenExecutor(),
        clock=FixedClock(T0),
        llm=LLM,
        max_calls=5,
        log=structlog.get_logger(),
    )
    report = runner.run_pass()
    assert report.counts == {"error": 2}
    assert report.batch_error == "RuntimeError: sandbox create failed"
    assert len(runner.pending()) == 2  # both wait for the next pass


def test_only_the_three_detection_agents_are_narrated(ledger: SqliteLedger) -> None:
    _raise(ledger, _finding("1001", agent_id="rbqm.kri_sentinel"))
    _raise(ledger, _finding("1002", agent_id="rbqm.csm"))
    pending = _runner(ledger, FakeModelClient()).pending()
    assert [e.seq for e in pending] == [2]


def test_cuts_filter_the_findings(ledger: SqliteLedger) -> None:
    _raise(ledger, _finding("1001"), cut="DC-BASE")
    _raise(ledger, _finding("1002"), cut="DC-NULL-S1")
    runner = _runner(ledger, FakeModelClient())
    assert [e.seq for e in runner.pending({"DC-BASE"})] == [1]
    assert len(runner.pending()) == 2


def test_a_mismatched_request_hash_from_the_sandbox_is_an_error(ledger: SqliteLedger) -> None:
    _raise(ledger, _finding("1016"))

    class Tampering(InProcessNarrationExecutor):
        def call(self, requests: Sequence[ModelRequest], timeout_seconds: float) -> NarrationBatch:
            batch = super().call(requests, timeout_seconds)
            changed = batch.results[0].model_copy(update={"request_sha256": "0" * 64})
            return NarrationBatch(results=(changed,), executor="inprocess")

    runner = NarratorRunner(
        ledger=ledger,
        executor=Tampering(FakeModelClient(script=[_good("1016")])),
        clock=FixedClock(T0),
        llm=LLM,
        max_calls=5,
        log=structlog.get_logger(),
    )
    assert runner.run_pass().counts == {"error": 1}
