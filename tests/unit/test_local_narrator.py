"""safety.local_narrator: validator 1.1.0 reused, the local rules, and the pass."""

import json
from pathlib import Path

import pytest
import structlog

from meridian.agents.local_model import InProcessLocalModelExecutor
from meridian.agents.rbqm import narrator
from meridian.agents.safety import local_narrator as ln
from meridian.agents.safety.local_narration import LocalNarratorRunner
from meridian.domain.enums import EventType
from meridian.domain.events import Event, FindingNarrative, FindingRaised
from meridian.domain.findings import Finding
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.llm.local import FakeLocalModelClient, LocalModelResponse
from meridian.platform.clock import FixedClock
from tests.local_model_support import DIGEST, MODEL, RECODED, SETTINGS, raise_spec
from tests.support import T0, make_ledger

EM_DASH = chr(0x2014)
HEADLINE = "Site 9001: one serious AE is coded to two preferred terms"
EXPLANATION = (
    "Subject 201-9001-001 has a serious AE coded HYPERSENSITIVITY in the clinical database "
    "and INFUSION RELATED HYPERSENSITIVITY REACTION in its probable case, both with onset "
    "2025-03-14."
)


@pytest.fixture
def ledger(tmp_path: Path) -> SqliteLedger:
    return make_ledger(tmp_path / "l.sqlite3", FixedClock(T0))


@pytest.fixture
def finding(ledger: SqliteLedger) -> Finding:
    (event,) = raise_spec(ledger, RECODED)
    assert isinstance(event.payload, FindingRaised)
    return event.payload.finding


def _response(text: str, thinking: int = 0, done: str = "stop") -> LocalModelResponse:
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


def _say(headline: str = HEADLINE, explanation: str = EXPLANATION) -> str:
    return json.dumps({"headline": headline, "explanation": explanation})


def test_an_answer_that_follows_the_contract_is_accepted_with_its_subject_id(
    finding: Finding,
) -> None:
    v = ln.validate(_response(_say()), ln.narrator_input(finding))
    assert v.reasons == ()
    assert (v.headline, v.explanation) == (HEADLINE, EXPLANATION)


@pytest.mark.parametrize(
    ("explanation", "reason"),
    [
        (EXPLANATION.replace("201-9001-001", "201-9001-002"), "subject_not_in_finding"),
        (EXPLANATION.replace("2025-03-14", "2025-03-15"), "date_not_displayed:2025-03-15"),
        (EXPLANATION.replace("both with", "4.5 days apart, both with"), "number_not_displayed"),
        (EXPLANATION.replace(", both", f" {EM_DASH} both"), "em_dash"),
        (EXPLANATION + " It was reported late. It was also severe.", "too_many_sentences"),
        (EXPLANATION + " According to the summary, it is one event", "misattribution"),
    ],
)
def test_the_rbqm_narrator_rules_and_the_local_rules_apply(
    finding: Finding, explanation: str, reason: str
) -> None:
    v = ln.validate(_response(_say(explanation=explanation)), ln.narrator_input(finding))
    assert v.headline is None
    assert v.explanation is None
    assert reason in v.reasons or reason in narrator.reason_codes(v.reasons), v.reasons


def test_the_site_must_be_named_and_a_subject_id_does_not_name_it(finding: Finding) -> None:
    """The site id inside the subject id (201-9001-001) does not count as naming Site 9001."""
    v = ln.validate(
        _response(_say(headline="One serious AE is coded to two preferred terms")),
        ln.narrator_input(finding),
    )
    assert v.reasons == ("missing_site",)


def test_a_reasoning_trace_or_a_truncated_answer_is_rejected(finding: Finding) -> None:
    inp = ln.narrator_input(finding)
    assert "thinking_present" in ln.validate(_response(_say(), thinking=40), inp).reasons
    assert "truncated" in ln.validate(_response(_say(), done="length"), inp).reasons


def test_only_safety_findings_of_the_narrated_version(finding: Finding) -> None:
    assert ln.may_narrate(finding)
    assert not ln.may_narrate(finding.model_copy(update={"agent_version": "1.1.0"}))
    rbqm = finding.model_copy(update={"agent_id": "rbqm.kri_monitor", "agent_version": "2.2.0"})
    assert not ln.may_narrate(rbqm)
    # The cloud narrator still never receives a safety finding.
    assert not narrator.may_narrate(finding)


def test_a_pass_records_the_narrative_with_model_and_digest(
    ledger: SqliteLedger, finding: Finding
) -> None:
    client = FakeLocalModelClient(script=[_say()], digests={MODEL: DIGEST})
    runner = LocalNarratorRunner(
        ledger=ledger,
        executor=InProcessLocalModelExecutor(client),
        clock=FixedClock(T0),
        settings=SETTINGS,
        log=structlog.get_logger(),
    )
    report = runner.run_pass()
    assert report.counts == {"accepted": 1}
    (event,) = [e for e in ledger.read(event_types=[EventType.FINDING_NARRATIVE])]
    assert isinstance(event, Event)
    assert isinstance(event.payload, FindingNarrative)
    p = event.payload
    assert (p.agent_id, p.agent_version, p.model, p.model_digest) == (
        "safety.local_narrator",
        "1.0.0",
        MODEL,
        DIGEST,
    )
    assert (p.headline, p.explanation) == (HEADLINE, EXPLANATION)
    assert p.finding_id == finding.finding_id
    # Subject ids may be sent here: nothing leaves the machine.
    (request,) = client.requests
    assert "201-9001-001" in request.user
    assert verify(ledger).ok
    assert runner.run_pass().counts == {}
