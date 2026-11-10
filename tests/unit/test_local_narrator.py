"""safety.local_narrator: validator 1.1.0 reused, the local rules, and the pass."""

import json
from pathlib import Path

import pytest
import structlog

from meridian.agents.local_model import InProcessLocalModelExecutor, LocalBatch
from meridian.agents.rbqm import narrator
from meridian.agents.safety import local_narrator as ln
from meridian.agents.safety.local_narration import LocalNarratorRunner
from meridian.domain.enums import EventType
from meridian.domain.events import Event, FindingNarrative, FindingRaised
from meridian.domain.findings import Finding
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.llm.local import FakeLocalModelClient, LocalModelResponse
from meridian.llm.local_job import LocalModelJob
from meridian.platform.clock import FixedClock
from tests.local_model_support import DIGEST, MODEL, RECODED, SETTINGS, raise_spec, unpaired_spec
from tests.support import T0, make_ledger

EM_DASH = chr(0x2014)
CHECK = "sae_term_mismatch"
OPENING = "Site 9001, subject 201-9001-001: preferred term differs from the safety case"
EXPLANATION = (
    "The serious AE is coded HYPERSENSITIVITY in the clinical database and INFUSION RELATED "
    "HYPERSENSITIVITY REACTION in its probable case, both with onset 14 Mar 2025."
)


@pytest.fixture
def ledger(tmp_path: Path) -> SqliteLedger:
    return make_ledger(tmp_path / "l.sqlite3", FixedClock(T0))


@pytest.fixture
def finding(ledger: SqliteLedger) -> Finding:
    (event,) = raise_spec(ledger, RECODED)
    assert isinstance(event.payload, FindingRaised)
    return event.payload.finding


def _inp(finding: Finding) -> dict[str, object]:
    return ln.narrator_input(finding, CHECK)


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


def _say(explanation: str = EXPLANATION) -> str:
    return json.dumps({"explanation": explanation})


def test_the_host_writes_the_opening_and_the_model_only_the_explanation(
    finding: Finding,
) -> None:
    inp = _inp(finding)
    assert inp["opening"] == OPENING
    v = ln.validate(_response(_say()), inp)
    assert v.reasons == ()
    assert (v.headline, v.explanation) == (OPENING, EXPLANATION)
    # A headline from the model is not part of the 1.0.1 contract.
    both = json.dumps({"headline": "Site 9001: x", "explanation": EXPLANATION})
    assert ln.validate(_response(both), inp).reasons == ("bad_json",)


def test_the_opening_names_site_subject_and_check(finding: Finding) -> None:
    assert ln.opening(finding, "late_sae_report") == (
        "Site 9001, subject 201-9001-001: serious AE reported to the sponsor late"
    )
    site_level = finding.model_copy(
        update={
            "evidence": tuple(
                e.model_copy(update={"selector": "SITEID=9001"}) for e in finding.evidence
            )
        }
    )
    assert ln.opening(site_level, "sae_late_reporting_site") == (
        "Site 9001: serious AEs reported to the sponsor late"
    )
    assert ln.opening(finding, "some_new_check").endswith(": some new check")
    assert all(name == name.lower() or "AE" in name for name in ln.PLAIN_NAMES.values())


def test_missing_site_stays_as_a_guard_on_the_opening(finding: Finding) -> None:
    """The site is in the opening by construction; if the opening ever lacked it, the
    validator would still reject (the site id inside 201-9001-001 does not count)."""
    inp = {**_inp(finding), "opening": "Subject 201-9001-001: preferred term differs"}
    assert ln.validate(_response(_say()), inp).reasons == ("missing_site",)


@pytest.mark.parametrize(
    ("explanation", "reason"),
    [
        (EXPLANATION.replace("The serious AE", "201-9001-002's AE"), "subject_not_in_finding"),
        (EXPLANATION.replace("14 Mar 2025", "15 Mar 2025"), "date_not_displayed:15 Mar 2025"),
        (EXPLANATION.replace("14 Mar 2025", "2025-03-14"), "date_not_displayed:2025-03-14"),
        (EXPLANATION.replace("14 Mar 2025", "March 14, 2025"), "date_not_displayed"),
        (EXPLANATION.replace("both with", "4.5 days apart, both with"), "number_not_displayed"),
        (EXPLANATION.replace(", both", f" {EM_DASH} both"), "em_dash"),
        (EXPLANATION + " It was reported late. It was also severe.", "too_many_sentences"),
        (EXPLANATION + " According to the summary, it is one event", "misattribution"),
    ],
)
def test_the_rbqm_narrator_rules_and_the_local_rules_apply(
    finding: Finding, explanation: str, reason: str
) -> None:
    v = ln.validate(_response(_say(explanation=explanation)), _inp(finding))
    assert v.headline is None
    assert v.explanation is None
    assert reason in v.reasons or reason in narrator.reason_codes(v.reasons), v.reasons


def test_the_narrator_input_has_readable_dates_only(finding: Finding) -> None:
    inp = _inp(finding)
    text = json.dumps(inp)
    assert not ln.ISO_DATE.search(text)
    displays = [e["display"] for e in inp["evidence"]]  # type: ignore[attr-defined]
    assert displays.count("14 Mar 2025") == 2
    assert "onset 14 Mar 2025" in str(inp["summary"])
    assert ln.displayed_dates(inp) == {"14 Mar 2025"}


@pytest.mark.parametrize(
    ("iso", "shown"),
    [
        ("2013-01-20", "20 Jan 2013"),
        ("2013-01-20T22:02:00+00:00", "20 Jan 2013, 22:02 UTC"),
        ("2013-01-20T22:02Z", "20 Jan 2013, 22:02 UTC"),
        ("2013-01-21T03:32:00+05:30", "20 Jan 2013, 22:02 UTC"),  # converted to UTC
        ("2013-01-20T22:02:15+00:00", "20 Jan 2013, 22:02:15 UTC"),
        ("2013-03-05T12:40", "5 Mar 2013, 12:40"),  # no offset: no zone is claimed
        (
            "awareness 2012-09-15T23:34:00+00:00, receipt 2012-09-24T05:34:00+00:00",
            "awareness 15 Sep 2012, 23:34 UTC, receipt 24 Sep 2012, 05:34 UTC",
        ),
    ],
)
def test_readable_dates(iso: str, shown: str) -> None:
    assert ln.readable(iso) == shown


def test_a_date_of_a_displayed_date_time_may_be_written_alone() -> None:
    inp = {"evidence": [{"display": "20 Jan 2013, 22:02 UTC"}]}
    assert ln.displayed_dates(inp) == {"20 Jan 2013, 22:02 UTC", "20 Jan 2013"}


FILLER_REJECTED = [
    (
        "This discrepancy highlights a medium severity issue.",
        {"filler_sentence_severity", "filler_sentence_discrepancy"},
    ),
    ("This highlights an inconsistency between the records.", {"filler_sentence_discrepancy"}),
    ("There is a discrepancy between the two databases.", {"filler_sentence_discrepancy"}),
    ("This indicates a problem with the coding.", {"filler_sentence_discrepancy"}),
    ("The finding is of high severity.", {"filler_sentence_severity"}),
    ("This is a significant concern for the study.", {"filler_sentence_severity"}),
    ("It is important.", {"filler_sentence_severity"}),
    ("The finding requires attention.", {"filler_sentence_status"}),
    ("It should be reviewed by the safety team.", {"filler_sentence_status"}),
    ("The finding remains open.", {"filler_sentence_status"}),
]


@pytest.mark.parametrize(("sentence", "codes"), FILLER_REJECTED)
def test_filler_sentences_are_rejected_each_with_its_code(
    finding: Finding, sentence: str, codes: set[str]
) -> None:
    v = ln.validate(_response(_say(f"{EXPLANATION} {sentence}")), _inp(finding))
    assert v.explanation is None
    filler = {r for r in v.reasons if r.startswith("filler_sentence")}
    assert filler == codes, v.reasons


FILLER_ACCEPTED = [
    # A sentence that names the evidence is not filler, whatever words it uses.
    "The terms are HYPERSENSITIVITY and INFUSION RELATED HYPERSENSITIVITY REACTION, a mismatch "
    "of preferred terms.",
    "This discrepancy is between HYPERSENSITIVITY and INFUSION RELATED HYPERSENSITIVITY REACTION.",
    "Both records have onset 14 Mar 2025, and there is a mismatch in the coded term only.",
    "The subject's serious AE has a probable case with another preferred term.",
]


@pytest.mark.parametrize("explanation", FILLER_ACCEPTED)
def test_sentences_with_evidence_are_not_filler(finding: Finding, explanation: str) -> None:
    v = ln.validate(_response(_say(explanation)), _inp(finding))
    assert not [r for r in v.reasons if r.startswith("filler_sentence")], v.reasons
    assert v.reasons == ()


def test_a_reasoning_trace_or_a_truncated_answer_is_rejected(finding: Finding) -> None:
    inp = _inp(finding)
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
        "1.0.1",
        MODEL,
        DIGEST,
    )
    assert (p.headline, p.explanation) == (OPENING, EXPLANATION)
    assert p.finding_id == finding.finding_id
    # Subject ids may be sent here: nothing leaves the machine.
    (request,) = client.requests
    assert "201-9001-001" in request.user
    assert OPENING in request.user  # the model sees the opening it continues
    assert "2025-03-14" not in request.user
    assert verify(ledger).ok
    assert runner.run_pass().counts == {}


class _Counting(InProcessLocalModelExecutor):
    def __init__(self, client: FakeLocalModelClient) -> None:
        super().__init__(client)
        self.jobs: list[int] = []

    def call(self, job: LocalModelJob, timeout_seconds: float) -> LocalBatch:
        self.jobs.append(len(job.requests))
        return super().call(job, timeout_seconds)


def test_a_pass_is_split_into_jobs_of_max_per_job(ledger: SqliteLedger) -> None:
    """One exec of 77 calls was cut by the gateway, so each job holds at most max_per_job."""
    raise_spec(ledger, unpaired_spec(3))  # two findings to narrate
    raise_spec(ledger, RECODED)  # and a third
    executor = _Counting(FakeLocalModelClient(script=["x"] * 3, digests={MODEL: DIGEST}))
    runner = LocalNarratorRunner(
        ledger=ledger,
        executor=executor,
        clock=FixedClock(T0),
        settings=SETTINGS.model_copy(update={"max_per_job": 2}),
        log=structlog.get_logger(),
    )
    report = runner.run_pass()
    assert executor.jobs == [2, 1]
    assert report.counts == {"rejected": 3}
