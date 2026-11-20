"""selector_leak (validator 1.1.1, both narrators): text that copies evidence selectors or
field names is rejected. The leaking texts are explanations safety.local_narrator 1.0.1
had accepted in step 3b2; the clean ones are accepted 1.0.1 texts without a selector."""

import json
from typing import Any

import pytest

from meridian.agents.rbqm import narrator
from meridian.agents.safety import local_narrator as ln
from meridian.llm.local import LocalModelResponse


def _ev(selector: str, statistic: str, display: str, role: str = "primary") -> dict[str, Any]:
    return {
        "source": "ae",
        "selector": selector,
        "statistic": statistic,
        "role": role,
        "direction": "none",
        "expected": None,
        "display": display,
    }


ORPHAN = {
    "site_id": "1001",
    "evidence": [
        _ev(
            "CASEID=SC-A4C2754345; VERSION=1; USUBJID=201-1001-025",
            "seriousness:safety_db",
            "serious case",
        ),
        _ev("USUBJID=201-1001-025; AEDECOD=CONFUSIONAL STATE", "seriousness:clinical", "no AE"),
    ],
}
NON_SERIOUS = {
    "site_id": "1016",
    "evidence": [
        _ev("USUBJID=201-1016-004; AESEQ=17; AESER=N", "seriousness:clinical", "non-serious"),
        _ev("CASEID=SC-0A1B2C3D4E; VERSION=1", "onset:safety_db", "21 May 2014", "supporting"),
    ],
}

LEAKS = [
    (  # SAF-0334
        ORPHAN,
        "The primary evidence shows a serious case of confusional state (CASEID=SC-A4C2754345; "
        "VERSION=1; USUBJID=201-1001-025) with no matching adverse event in the clinical "
        "database.",
        {"CASEID", "VERSION", "USUBJID"},
    ),
    (  # SAF-0339
        ORPHAN,
        "The primary evidence shows a serious case of Syncope (CASEID=SC-E0BED23ED1) with no "
        "matching adverse event in the clinical database.",
        {"CASEID"},
    ),
    (  # SAF-0322
        NON_SERIOUS,
        "The primary evidence shows a serious case of back pain (AESEQ 17.0, AESER = N) "
        "recorded as non-serious in the clinical database.",
        {"AESEQ", "AESER"},
    ),
    (  # SAF-0352: a misspelt field and an invented key are still key=value
        NON_SERIOUS,
        "A contusion (AESEQ=7) reported on 6 Jul 2014 (AESEER=Y) is serious (SEVERITY=high).",
        {"AESEQ", "AESEER", "SEVERITY"},
    ),
    (  # a statistic written out
        NON_SERIOUS,
        "The onset:safety_db value is 21 May 2014.",
        {"onset:safety_db"},
    ),
]


@pytest.mark.parametrize(("inp", "text", "keys"), LEAKS)
def test_copied_selectors_and_field_names_are_leaks(
    inp: dict[str, Any], text: str, keys: set[str]
) -> None:
    assert set(narrator.selector_leaks(text, inp)) == keys


CLEAN = [
    (  # SAF-0319 (accepted, shown in the step 3b2 report)
        NON_SERIOUS,
        "The site became aware of Arthralgia on 15 Sep 2012, 23:34 UTC and reported it to the "
        "sponsor 198 hours later on 24 Sep 2012, 05:34 UTC, which is 8.2 days.",
    ),
    (  # SAF-0325
        ORPHAN,
        "The causality of syncope in subject 201-1009-023 is POSSIBLE in the clinical database "
        "but NONE in the safety case.",
    ),
    (  # an outcome in capitals and a time: neither is a key
        NON_SERIOUS,
        "The outcome is NOT RECOVERED/NOT RESOLVED in case SC-0A1B2C3D4E at 07:43.",
    ),
]


@pytest.mark.parametrize(("inp", "text"), CLEAN)
def test_ordinary_text_is_not_a_leak(inp: dict[str, Any], text: str) -> None:
    assert narrator.selector_leaks(text, inp) == []


def test_both_validators_reject_a_leak_with_its_code() -> None:
    text = LEAKS[1][1]
    cloud = narrator.validate(
        json.dumps({"headline": "Site 1001: a case has no clinical SAE", "explanation": text}),
        ORPHAN,
    )
    assert "selector_leak:CASEID" in cloud.reasons
    response = LocalModelResponse(
        text=json.dumps({"explanation": text}),
        model="m",
        done_reason="stop",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        response_sha256="0" * 64,
    )
    local = ln.validate(
        response, {**ORPHAN, "opening": "Site 1001: Safety case with no serious AE"}
    )
    assert "selector_leak:CASEID" in local.reasons
    assert narrator.reason_codes(local.reasons).count("selector_leak") == 1
    assert (narrator.VERSION, ln.VERSION) == ("1.1.1", "1.0.2")
