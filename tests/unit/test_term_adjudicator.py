"""safety.term_adjudicator with the fake local model client: candidates, the input, the
validator, digest pinning, and that no finding is ever altered."""

import json
import re
from pathlib import Path

import pytest
import structlog
from pydantic import ValidationError

from meridian.agents.local_model import InProcessLocalModelExecutor
from meridian.agents.rbqm.narrator_v1_0 import SUBJECT_ID
from meridian.agents.safety import term_adjudicator as ta
from meridian.agents.safety.adjudication import AdjudicatorRunner
from meridian.domain.enums import EventType, Visibility
from meridian.domain.events import Event, FindingAssessment, FindingRaised
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.llm.local import FakeLocalModelClient, LocalModelResponse
from meridian.platform.clock import FixedClock
from tests.local_model_support import (
    DIGEST,
    MODEL,
    RECODED,
    SETTINGS,
    answer,
    fake,
    raise_spec,
    spec_lookup,
    unpaired_spec,
)
from tests.support import T0, make_ledger

EM_DASH = chr(0x2014)


@pytest.fixture
def ledger(tmp_path: Path) -> SqliteLedger:
    return make_ledger(tmp_path / "l.sqlite3", FixedClock(T0))


def _runner(
    ledger: SqliteLedger, client: FakeLocalModelClient, spec: dict[str, object] = RECODED
) -> AdjudicatorRunner:
    return AdjudicatorRunner(
        ledger=ledger,
        executor=InProcessLocalModelExecutor(client),
        clock=FixedClock(T0),
        settings=SETTINGS,
        lookup=spec_lookup(spec),
        window_days=7,
        log=structlog.get_logger(),
    )


def _assessments(ledger: SqliteLedger) -> list[tuple[Event, FindingAssessment]]:
    out = []
    for e in ledger.read(event_types=[EventType.FINDING_ASSESSMENT]):
        assert isinstance(e, Event)
        assert isinstance(e.payload, FindingAssessment)
        out.append((e, e.payload))
    return out


def test_a_term_mismatch_is_one_candidate(ledger: SqliteLedger) -> None:
    raised = raise_spec(ledger, RECODED)
    (c,) = ta.candidates(raised, window_days=7)
    assert c.kind == "term_mismatch"
    assert len(c.finding_ids) == 1
    assert (c.clinical.usubjid, c.clinical.aeseq) == ("201-9001-001", 1)
    assert c.case.caseid == "SC-ST01A"


def test_a_missing_case_and_an_orphan_within_the_window_are_one_candidate(
    ledger: SqliteLedger,
) -> None:
    raised = raise_spec(ledger, unpaired_spec(3))
    checks = sorted(ta.check_of(e) for e in raised)
    assert checks == ["case_without_clinical_sae", "sae_missing_in_safety_db"]
    (c,) = ta.candidates(raised, window_days=7)
    assert c.kind == "unpaired_pair"
    assert len(c.finding_ids) == 2
    assert list(c.finding_ids) == sorted(c.finding_ids)


def test_an_orphan_outside_the_window_is_no_candidate(ledger: SqliteLedger) -> None:
    raised = raise_spec(ledger, unpaired_spec(9))
    assert ta.candidates(raised, window_days=7) == []


@pytest.mark.parametrize("spec", [RECODED, unpaired_spec(3)], ids=["mismatch", "pair"])
def test_the_input_holds_the_four_terms_and_no_date_or_id(
    ledger: SqliteLedger, spec: dict[str, object]
) -> None:
    raise_spec(ledger, spec)
    client = fake(answer())
    _runner(ledger, client, spec).run_pass()
    (request,) = client.requests
    user = request.user
    inp = json.loads(user.removeprefix("Records:\n"))
    assert inp == {
        "record_a": {
            "preferred_term": "HYPERSENSITIVITY",
            "verbatim_term": "allergic reaction to infusion",
        },
        "record_b": {
            "preferred_term": "INFUSION RELATED HYPERSENSITIVITY REACTION",
            "verbatim_term": "allergic reaction to infusion",
        },
    }
    whole = request.system + user
    assert not SUBJECT_ID.search(whole)
    assert "SC-ST01A" not in whole
    assert not re.search(r"\d{4}-\d{2}", whole)
    assert "9001" not in whole  # nor the site


def test_input_problems_refuse_a_date_or_an_id_in_a_term() -> None:
    good = ta.adjudicator_input(ta.Term("SYNCOPE", "fainted"), ta.Term("SYNCOPE", "fainting"))
    assert ta.input_problems(good) == []
    dated = ta.adjudicator_input(ta.Term("SYNCOPE", "fainted 2025-03-14"), ta.Term("A", "b"))
    assert ta.input_problems(dated) == ["date"]
    subject = ta.adjudicator_input(ta.Term("SYNCOPE", "201-9001-001 fainted"), ta.Term("A", "b"))
    assert "subject_id" in ta.input_problems(subject)
    assert ta.input_problems({**good, "onset": "x"}) == ["unexpected_field"]


def test_an_accepted_assessment_never_alters_a_finding(ledger: SqliteLedger) -> None:
    raised = raise_spec(ledger, RECODED)
    before = [e.model_dump_json() for e in ledger.read()]
    report = _runner(ledger, fake(answer("yes"))).run_pass()
    assert report.counts == {"accepted": 1}
    after = list(ledger.read())
    # Every earlier event is byte-for-byte the same; the only new one is the assessment.
    assert [e.model_dump_json() for e in after[: len(before)]] == before
    assert [e.event_type for e in after[len(before) :]] == [EventType.FINDING_ASSESSMENT]
    ((event, a),) = _assessments(ledger)
    assert (a.status, a.verdict, a.candidate) == ("accepted", "yes", "term_mismatch")
    assert a.reason == "Both name the same allergic reaction."
    assert a.finding_ids == (raised[0].payload.finding.finding_id,)  # type: ignore[union-attr]
    assert a.finding_seqs == (raised[0].seq,)
    assert (a.model, a.model_digest, a.executor) == (MODEL, DIGEST, "inprocess")
    assert a.request_sha256 is not None
    assert a.response_sha256 is not None
    assert (a.input_tokens, a.output_tokens, a.latency_ms) == (120, 30, 1)
    assert event.visibility is Visibility.BLINDED
    assert event.actor == "safety.term_adjudicator"
    assert verify(ledger).ok
    # Final: the next pass sends nothing.
    assert _runner(ledger, fake()).run_pass().counts == {}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("not json", "bad_json"),
        (json.dumps({"same_event": "maybe", "reason": "r."}), "bad_verdict"),
        (json.dumps({"same_event": "yes"}), "bad_json"),
        (json.dumps({"same_event": "yes", "reason": "r.", "extra": 1}), "bad_json"),
        (json.dumps({"same_event": "yes", "reason": " "}), "reason_empty"),
        (answer("yes", "One. Two."), "too_many_sentences"),
        (answer("yes", "x" * 201), "reason_too_long"),
        (answer("yes", f"Same event {EM_DASH} both allergic."), "em_dash"),
    ],
)
def test_invalid_output_is_rejected_recorded_and_never_shown(
    ledger: SqliteLedger, text: str, reason: str
) -> None:
    raise_spec(ledger, RECODED)
    report = _runner(ledger, fake(text)).run_pass()
    assert report.counts == {"rejected": 1}
    ((_, a),) = _assessments(ledger)
    assert reason in a.rejection_reasons
    assert (a.verdict, a.reason) == (None, None)
    assert a.response_sha256 is not None


def test_a_reasoning_trace_is_rejected_and_never_reaches_the_validator(
    ledger: SqliteLedger,
) -> None:
    raise_spec(ledger, RECODED)
    client = fake(answer("yes"), thinking="Thinking Process: the user wants a verdict.")
    _runner(ledger, client).run_pass()
    ((_, a),) = _assessments(ledger)
    assert a.status == "rejected"
    assert a.rejection_reasons == ("thinking_present",)
    assert "Thinking" not in a.model_dump_json()


def test_validate_reads_only_the_content() -> None:
    def response(text: str, thinking: int = 0, done: str = "stop") -> LocalModelResponse:
        return LocalModelResponse(
            text=text,
            model=MODEL,
            done_reason=done,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            response_sha256="0" * 64,
            thinking_chars=thinking,
        )

    ok = ta.validate(response(answer("unsure", "The terms may differ.")))
    assert (ok.verdict, ok.reason, ok.reasons) == ("unsure", "The terms may differ.", ())
    assert ta.validate(response(answer(), done="length")).reasons == ("truncated",)
    assert ta.validate(response("```json\n" + answer("no", "Different.") + "\n```")).verdict == "no"


def test_a_different_served_digest_sends_nothing_and_is_retried(ledger: SqliteLedger) -> None:
    raise_spec(ledger, RECODED)
    client = fake(answer(), digest="b" * 64)
    report = _runner(ledger, client).run_pass()
    assert client.requests == []
    assert report.counts == {"error": 1}
    ((_, a),) = _assessments(ledger)
    assert a.error is not None
    assert a.error.startswith("digest_mismatch")
    assert a.model_digest == "b" * 64
    # An error is not final: the next pass with the pinned model judges it.
    assert _runner(ledger, fake(answer("no", "Different conditions."))).run_pass().counts == {
        "accepted": 1
    }


def test_an_assessment_round_trips_and_its_rules_hold() -> None:
    a = FindingAssessment(
        finding_ids=("SAF-0001", "SAF-0002"),
        finding_seqs=(3, 4),
        candidate="unpaired_pair",
        agent_id="safety.term_adjudicator",
        agent_version="1.0.0",
        status="accepted",
        verdict="unsure",
        reason="The terms may describe the same event.",
        model=MODEL,
        model_digest=DIGEST,
        request_sha256="c" * 64,
        response_sha256="d" * 64,
    )
    assert FindingAssessment.model_validate_json(a.model_dump_json()) == a
    with pytest.raises(ValidationError):
        a.model_copy(update={"verdict": None}).model_validate({**a.model_dump(), "verdict": None})
    with pytest.raises(ValidationError, match="wrong number"):
        FindingAssessment.model_validate({**a.model_dump(), "candidate": "term_mismatch"})
    with pytest.raises(ValidationError, match="records no verdict"):
        FindingAssessment.model_validate(
            {**a.model_dump(), "status": "rejected", "rejection_reasons": ("bad_json",)}
        )
    with pytest.raises(ValidationError, match="digest"):
        FindingAssessment.model_validate({**a.model_dump(), "model_digest": None})


def test_only_the_adjudicated_reconciler_version_is_a_candidate(ledger: SqliteLedger) -> None:
    raised = raise_spec(ledger, RECODED)
    finding = raised[0].payload.finding  # type: ignore[union-attr]
    assert ta.may_adjudicate(finding)
    assert not ta.may_adjudicate(finding.model_copy(update={"agent_version": "1.1.0"}))
    assert isinstance(raised[0].payload, FindingRaised)
