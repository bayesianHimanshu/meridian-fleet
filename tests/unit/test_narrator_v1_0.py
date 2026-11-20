"""rbqm.narrator 1.0.0 (frozen): input, de-identification, prompt and validation."""

import json
from typing import Any

import pytest

from meridian.agents.rbqm import narrator_v1_0 as n
from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.findings import Evidence, Finding
from meridian.ledger.hashing import canonical_json

# The real DC-BASE under-reporting finding for site 1005 (RBQM-0027), trimmed.
FINDING = Finding(
    finding_id="RBQM-0027",
    kind=FindingKind.UNDER_REPORTING,
    agent_id="rbqm.kri_monitor",
    agent_version="1.0.0",
    study_id="MRD-201",
    site_id="1005",
    severity=Severity.HIGH,
    title="Site 1005: fewer adverse events than expected",
    summary=(
        "Site 1005 has 27 adverse events over 2075 subject-days; the rest of the study "
        "implies 70.2. Poisson p = 3.35e-09, 4.35e-08 after Bonferroni over 13 sites."
    ),
    evidence=(
        Evidence(
            source="blinded/ae",
            selector="SITEID=1005",
            statistic="ae_rate:observed",
            value=27.0,
        ),
        Evidence(
            source="blinded/ae",
            selector="SITEID=1005",
            statistic="ae_rate:expected",
            value=70.22649957840258,
        ),
        Evidence(
            source="blinded/ae",
            selector="SITEID=1005",
            statistic="ae_rate:per_100_subject_days",
            value=1.3012048192771084,
            threshold=3.384409618236269,
        ),
        Evidence(
            source="blinded/ae",
            selector="SITEID=1005",
            statistic="ae_rate:poisson_p_adjusted",
            value=4.349480604376574e-08,
            threshold=0.01,
        ),
    ),
    visibility=Visibility.BLINDED,
    visible_to=frozenset({Role.CLINICAL_OPS_LEAD}),
    score=1.0,
)
INPUT = n.narrator_input(FINDING)
GOOD = {
    "headline": "Site 1005 reports far fewer adverse events than expected",
    "explanation": (
        "Site 1005 recorded 27 adverse events where about 70.2 were expected from the rest "
        "of the study. The rate is 1.30 per 100 subject-days against a threshold of 3.38."
    ),
}


def _reply(**changes: str) -> str:
    return json.dumps(GOOD | changes)


# ---- input and de-identification


def test_the_input_holds_only_the_allowed_fields() -> None:
    assert set(INPUT) == n.ALLOWED_FIELDS
    assert all(set(e) == n.ALLOWED_EVIDENCE_FIELDS for e in INPUT["evidence"])
    assert "finding_id" not in canonical_json(INPUT)
    assert n.deid_problems(canonical_json(INPUT)) == []


def _with(**changes: Any) -> str:
    return canonical_json(INPUT | changes)


def test_a_subject_id_is_refused() -> None:
    serialized = _with(summary="Subject 201-1005-012 had 3 events.")
    assert n.deid_problems(serialized) == ["subject_id_pattern"]


def test_a_usubjid_key_is_refused() -> None:
    evidence = [{**INPUT["evidence"][0], "USUBJID": "x"}]
    assert "forbidden_key:USUBJID" in n.deid_problems(_with(evidence=evidence))


def test_a_src_usubjid_key_in_a_selector_is_refused() -> None:
    evidence = [{**INPUT["evidence"][0], "selector": "SRC_USUBJID=01-701-1015"}]
    assert n.deid_problems(_with(evidence=evidence)) == ["forbidden_key:SRC_USUBJID"]


def test_a_field_outside_the_allowed_list_is_refused() -> None:
    assert n.deid_problems(_with(cut_path="data/curated/x")) == ["field_not_allowed:cut_path"]
    evidence = [{**INPUT["evidence"][0], "subjects": [1, 2]}]
    assert n.deid_problems(_with(evidence=evidence)) == ["field_not_allowed:evidence.subjects"]


def test_a_number_that_only_looks_like_an_id_is_not_refused() -> None:
    assert n.deid_problems(_with(summary="2201-1005-0123 is not a subject id")) == []


# ---- prompt


def test_the_request_carries_the_input_and_the_settings() -> None:
    req = n.build_request(
        INPUT, model="claude-haiku-5-5", max_tokens=300, temperature=0.0, disable_thinking=True
    )
    body = req.body()
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 300
    assert body["temperature"] == 0.0
    assert body["messages"][0]["content"].endswith(canonical_json(INPUT))
    assert n.EM_DASH not in body["system"]
    assert "em dash" in body["system"]


# ---- validation


def test_a_good_reply_is_accepted() -> None:
    v = n.validate(_reply(), INPUT)
    assert v.accepted, v.reasons
    assert v.headline == GOOD["headline"]


def test_a_fenced_reply_is_tolerated() -> None:
    assert n.validate("```json\n" + _reply() + "\n```", INPUT).accepted


@pytest.mark.parametrize(
    "text",
    [
        "Site 1005 has fewer events.",
        '{"headline": "x"}',
        '{"headline": "x", "explanation": "y", "advice": "z"}',
        '["Site 1005", "x"]',
        '{"headline": "", "explanation": "Site 1005"}',
    ],
)
def test_bad_json_is_rejected(text: str) -> None:
    assert n.validate(text, INPUT).reasons == ("bad_json",)


def test_a_number_not_in_the_evidence_is_rejected() -> None:
    v = n.validate(_reply(explanation="Site 1005 recorded 43 fewer events than expected."), INPUT)
    assert v.reasons == ("number_not_in_evidence:43",)
    assert v.headline is None


def test_rounding_needs_two_significant_digits() -> None:
    ok = "Site 1005 expected 70 events, the rate was 1.3 per 100 subject-days."
    assert n.validate(_reply(explanation=ok), INPUT).accepted
    wrong = "Site 1005 recorded about 1 event per 100 subject-days."
    assert n.validate(_reply(explanation=wrong), INPUT).reasons == ("number_not_in_evidence:1",)


def test_scientific_notation_and_percentages_match_at_their_precision() -> None:
    text = "Site 1005 has an adjusted p of 4.3e-08 and a rate 38.4% of the threshold."
    v = n.validate(_reply(explanation=text), INPUT)
    # 4.3e-08 rounds 4.35e-08 to 2 digits; 38.4% is not a rate in the evidence.
    assert v.reasons == ("number_not_in_evidence:38.4%",)


def test_a_missing_site_is_rejected() -> None:
    v = n.validate(_reply(headline="Fewer adverse events", explanation="27 events."), INPUT)
    assert v.reasons == ("missing_site",)


def test_a_site_id_inside_another_number_does_not_count() -> None:
    v = n.validate(_reply(headline="Site 10050", explanation="Site 10050 has 27 events."), INPUT)
    assert "missing_site" in v.reasons


def test_a_study_wide_finding_needs_no_site() -> None:
    study = INPUT | {"site_id": None}
    assert n.validate(_reply(headline="Study-wide", explanation="27 events."), study).accepted


def test_an_em_dash_is_rejected() -> None:
    text = "Site 1005 recorded 27 events " + n.EM_DASH + " far fewer than 70.2."
    assert n.validate(_reply(explanation=text), INPUT).reasons == ("em_dash",)


def test_too_long_is_rejected() -> None:
    assert n.validate(_reply(headline="Site 1005 " + "x" * 120), INPUT).reasons == ("too_long",)
    long = "Site 1005 recorded 27 adverse events. " + "Far fewer than expected " * 20 + "."
    assert "too_long" in n.validate(_reply(explanation=long), INPUT).reasons


def test_three_sentences_are_rejected() -> None:
    text = "Site 1005 recorded 27 events. That is low. It was 70.2 expected."
    assert n.validate(_reply(explanation=text), INPUT).reasons == ("too_many_sentences",)


def test_decimals_do_not_end_sentences() -> None:
    assert n.sentence_count("Site 1005 has 1.30 events. Expected 70.2.") == 2


def test_a_subject_id_in_the_reply_is_rejected() -> None:
    text = "Site 1005 subject 201-1005-012 recorded 27 events."
    assert "subject_id" in n.validate(_reply(explanation=text), INPUT).reasons


def test_stop_reasons_are_reasons() -> None:
    assert n.validate(_reply(), INPUT, stop_reason="refusal").reasons == ("model_refusal",)
    truncated = n.validate('{"headline": "Site 1005', INPUT, stop_reason="max_tokens")
    assert truncated.reasons == ("truncated", "bad_json")


def test_every_reason_is_reported_once() -> None:
    text = "Site 9 had 43 and 43 events " + n.EM_DASH + " odd."
    v = n.validate(_reply(headline="Odd", explanation=text), INPUT)
    assert v.reasons == (
        "em_dash",
        "missing_site",
        "number_not_in_evidence:9",
        "number_not_in_evidence:43",
    )
