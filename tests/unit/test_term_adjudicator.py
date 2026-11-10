"""safety.term_adjudicator 1.1.0 with the fake local model client: candidates, the input,
the validator, the agreement rule over two models, digest pinning, and that no finding is
ever altered."""

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
from meridian.domain.events import (
    Event,
    FindingAssessment,
    FindingRaised,
    ModelAnswer,
    Verdict,
    agreed_verdict,
)
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.llm.local import FakeLocalModelClient, LocalModelResponse
from meridian.platform.clock import FixedClock
from tests.local_model_support import (
    DIGEST_A,
    DIGEST_B,
    MODEL,
    MODEL_A,
    MODEL_B,
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
BOTH = f"{MODEL_A}+{MODEL_B}"


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
    client = fake(answer(), answer())
    _runner(ledger, client, spec).run_pass()
    first, second = client.requests
    # Both models get the same question, with the 1.0.0 prompt.
    assert (first.model, second.model) == (MODEL_A, MODEL_B)
    assert (
        (first.system, first.user)
        == (second.system, second.user)
        == (
            ta.SYSTEM_PROMPT,
            first.user,
        )
    )
    assert first.think is second.think is False
    request = first
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
    report = _runner(ledger, fake(answer("yes"), answer("yes", "Same allergy."))).run_pass()
    assert report.counts == {"accepted": 1}
    after = list(ledger.read())
    # Every earlier event is byte-for-byte the same; the only new one is the assessment.
    assert [e.model_dump_json() for e in after[: len(before)]] == before
    assert [e.event_type for e in after[len(before) :]] == [EventType.FINDING_ASSESSMENT]
    ((event, a),) = _assessments(ledger)
    assert (a.status, a.verdict, a.candidate) == ("accepted", "yes", "term_mismatch")
    assert a.agent_version == "1.1.0"
    assert a.reason is not None
    assert a.reason.startswith("Every model said the terms describe the same event")
    assert a.finding_ids == (raised[0].payload.finding.finding_id,)  # type: ignore[union-attr]
    assert a.finding_seqs == (raised[0].seq,)
    assert (a.model, a.model_digest, a.request_sha256, a.executor) == (
        BOTH,
        None,
        None,
        "inprocess",
    )
    assert event.visibility is Visibility.BLINDED
    assert event.actor == "safety.term_adjudicator"
    assert verify(ledger).ok
    # Final: the next pass sends nothing.
    assert _runner(ledger, fake()).run_pass().counts == {}


def test_both_raw_answers_models_and_digests_are_recorded(ledger: SqliteLedger) -> None:
    raise_spec(ledger, RECODED)
    text_a = answer("yes", "Both name the same allergic reaction.")
    text_b = answer("no", "One names an infusion reaction only.")
    client = fake(text_a, text_b)
    _runner(ledger, client).run_pass()
    ((_, a),) = _assessments(ledger)
    first, second = a.answers
    assert (first.model, first.model_digest, first.text) == (MODEL_A, DIGEST_A, text_a)
    assert (second.model, second.model_digest, second.text) == (MODEL_B, DIGEST_B, text_b)
    assert (first.verdict, second.verdict) == ("yes", "no")
    assert first.reason == "Both name the same allergic reaction."
    assert first.request_sha256 == client.requests[0].sha256()
    assert second.request_sha256 == client.requests[1].sha256()
    assert first.response_sha256 is not None
    assert second.response_sha256 is not None
    assert (first.input_tokens, first.output_tokens, first.latency_ms) == (120, 30, 1)
    # They disagree, so the assessment abstains.
    assert (a.status, a.verdict) == ("accepted", "unsure")
    assert a.latency_ms == 2
    stored = FindingAssessment.model_validate_json(a.model_dump_json())
    assert stored.answers == a.answers


TRUTH_TABLE: list[tuple[Verdict | None, Verdict | None, Verdict]] = [
    ("yes", "yes", "yes"),
    ("no", "no", "no"),
    ("unsure", "unsure", "unsure"),
    ("yes", "no", "unsure"),
    ("no", "yes", "unsure"),
    ("yes", "unsure", "unsure"),
    ("unsure", "yes", "unsure"),
    ("no", "unsure", "unsure"),
    ("unsure", "no", "unsure"),
    ("yes", None, "unsure"),  # None: the answer failed validation
    (None, "no", "unsure"),
]


@pytest.mark.parametrize(("a", "b", "expected"), TRUTH_TABLE)
def test_the_agreement_rule_truth_table(
    a: Verdict | None, b: Verdict | None, expected: Verdict
) -> None:
    assert agreed_verdict((a, b)) == expected


@pytest.mark.parametrize(("a", "b", "expected"), TRUTH_TABLE)
def test_the_runner_applies_the_agreement_rule(
    ledger: SqliteLedger, a: Verdict | None, b: Verdict | None, expected: Verdict
) -> None:
    raise_spec(ledger, RECODED)
    texts = [answer(v, "About the terms.") if v else "not json" for v in (a, b)]
    _runner(ledger, fake(*texts)).run_pass()
    ((_, assessment),) = _assessments(ledger)
    assert (assessment.status, assessment.verdict) == ("accepted", expected)
    assert tuple(x.verdict for x in assessment.answers) == (a, b)


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
def test_invalid_output_is_rejected_and_recorded_raw(
    ledger: SqliteLedger, text: str, reason: str
) -> None:
    raise_spec(ledger, RECODED)
    # One invalid answer: no agreement, so the assessment abstains.
    _runner(ledger, fake(text, answer("yes"))).run_pass()
    ((_, a),) = _assessments(ledger)
    first, _second = a.answers
    assert (first.status, first.verdict, first.reason) == ("rejected", None, None)
    assert reason in first.rejection_reasons
    assert first.text == text  # the raw answer is kept
    assert (a.status, a.verdict) == ("accepted", "unsure")
    assert a.reason is not None
    assert "phi4-mini gave no valid answer" in a.reason


def test_two_invalid_answers_reject_the_assessment(ledger: SqliteLedger) -> None:
    raise_spec(ledger, RECODED)
    report = _runner(ledger, fake("not json", answer("yes", "x" * 201))).run_pass()
    assert report.counts == {"rejected": 1}
    ((_, a),) = _assessments(ledger)
    assert (a.verdict, a.reason) == (None, None)
    assert a.rejection_reasons == ("phi4-mini:bad_json", "qwen3.5:reason_too_long")


def test_a_reasoning_trace_is_rejected_and_never_reaches_the_validator(
    ledger: SqliteLedger,
) -> None:
    raise_spec(ledger, RECODED)
    client = fake(answer("yes"), answer("yes"), thinking="Thinking Process: the user wants.")
    _runner(ledger, client).run_pass()
    ((_, a),) = _assessments(ledger)
    assert a.status == "rejected"
    assert {x.rejection_reasons for x in a.answers} == {("thinking_present",)}
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
    client = fake(answer(), served={MODEL_B: "b" * 64})
    report = _runner(ledger, client).run_pass()
    assert [r.model for r in client.requests] == [MODEL_A]  # nothing sent to MODEL_B
    assert report.counts == {"error": 1}
    ((_, a),) = _assessments(ledger)
    assert a.error is not None
    assert a.error.startswith("qwen3.5: digest_mismatch")
    first, second = a.answers
    assert (first.status, second.status) == ("accepted", "error")
    assert second.text is None
    # An error is not final: the next pass with the pinned models judges it.
    again = _runner(ledger, fake(answer("no", "Different."), answer("no", "Different."))).run_pass()
    assert again.counts == {"accepted": 1}
    assert _assessments(ledger)[-1][1].verdict == "no"


def _answer(model: str, digest: str, verdict: Verdict) -> ModelAnswer:
    return ModelAnswer(
        model=model,
        model_digest=digest,
        status="accepted",
        verdict=verdict,
        reason="About the terms.",
        text=answer(verdict, "About the terms."),
        request_sha256="c" * 64,
        response_sha256="d" * 64,
    )


def test_an_assessment_round_trips_and_its_rules_hold() -> None:
    a = FindingAssessment(
        finding_ids=("SAF-0001", "SAF-0002"),
        finding_seqs=(3, 4),
        candidate="unpaired_pair",
        agent_id="safety.term_adjudicator",
        agent_version="1.1.0",
        status="accepted",
        verdict="unsure",
        reason="The models do not agree.",
        model=BOTH,
        answers=(_answer(MODEL_A, DIGEST_A, "yes"), _answer(MODEL_B, DIGEST_B, "no")),
    )
    assert FindingAssessment.model_validate_json(a.model_dump_json()) == a
    data = a.model_dump()
    with pytest.raises(ValidationError, match="wrong number"):
        FindingAssessment.model_validate({**data, "candidate": "term_mismatch"})
    with pytest.raises(ValidationError, match="agreed verdict"):
        FindingAssessment.model_validate({**data, "verdict": "yes"})
    with pytest.raises(ValidationError, match="names its answers"):
        FindingAssessment.model_validate({**data, "model": MODEL_A})
    with pytest.raises(ValidationError, match="per answer"):
        FindingAssessment.model_validate({**data, "model_digest": DIGEST_A})
    with pytest.raises(ValidationError, match="two or more answers"):
        FindingAssessment.model_validate({**data, "answers": data["answers"][:1]})
    with pytest.raises(ValidationError, match="make the assessment accepted"):
        FindingAssessment.model_validate(
            {**data, "status": "rejected", "verdict": None, "reason": None}
        )
    with pytest.raises(ValidationError, match="an answer that was received"):
        ModelAnswer.model_validate({**data["answers"][0], "text": None})
    # A 1.0.0 single-model assessment still validates, and still needs its digest.
    single = FindingAssessment(
        finding_ids=("SAF-0001",),
        finding_seqs=(3,),
        candidate="term_mismatch",
        agent_id="safety.term_adjudicator",
        agent_version="1.0.0",
        status="accepted",
        verdict="yes",
        reason="Same.",
        model=MODEL,
        model_digest="a" * 64,
        request_sha256="c" * 64,
    )
    with pytest.raises(ValidationError, match="digest"):
        FindingAssessment.model_validate({**single.model_dump(), "model_digest": None})
    with pytest.raises(ValidationError, match="request sha256"):
        FindingAssessment.model_validate({**single.model_dump(), "request_sha256": None})


def test_only_the_adjudicated_reconciler_version_is_a_candidate(ledger: SqliteLedger) -> None:
    raised = raise_spec(ledger, RECODED)
    finding = raised[0].payload.finding  # type: ignore[union-attr]
    assert ta.may_adjudicate(finding)
    assert not ta.may_adjudicate(finding.model_copy(update={"agent_version": "1.1.0"}))
    assert isinstance(raised[0].payload, FindingRaised)


def test_each_model_answers_all_its_requests_before_the_next(ledger: SqliteLedger) -> None:
    raise_spec(ledger, RECODED)
    raise_spec(ledger, unpaired_spec(3))  # a second candidate: a pair of the same terms
    client = fake(*(answer("yes") for _ in range(4)))
    runner = AdjudicatorRunner(
        ledger=ledger,
        executor=InProcessLocalModelExecutor(client),
        clock=FixedClock(T0),
        settings=SETTINGS.model_copy(update={"max_per_job": 1}),
        lookup=spec_lookup(RECODED),
        window_days=7,
        log=structlog.get_logger(),
    )
    assert runner.run_pass().counts == {"accepted": 2}
    # One job per request here, all of MODEL_A first: Ollama loads each model once.
    assert [r.model for r in client.requests] == [MODEL_A, MODEL_A, MODEL_B, MODEL_B]
    assert all(len(a.answers) == 2 for _, a in _assessments(ledger))
