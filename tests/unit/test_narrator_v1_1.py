"""rbqm.narrator 1.1.0: input from described evidence, and each validator rule."""

import json
from typing import Any

import pytest

from meridian.agents.rbqm import narrator as n
from meridian.agents.rbqm.describe import ratio_evidence, share_of_limit_evidence
from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.findings import Evidence, Finding
from meridian.ledger.hashing import canonical_json


def _finding(
    kind: FindingKind, site: str | None, title: str, summary: str, evidence: list[Evidence]
) -> Finding:
    return Finding(
        finding_id="RBQM-0001",
        kind=kind,
        agent_id="rbqm.kri_monitor",
        agent_version="2.2.0",
        study_id="MRD-201",
        site_id=site,
        severity=Severity.HIGH,
        title=title,
        summary=summary,
        evidence=tuple(evidence),
        visibility=Visibility.BLINDED,
        visible_to=frozenset({Role.CLINICAL_OPS_LEAD}),
        score=1.0,
    )


def _ev(statistic: str, value: float, threshold: float | None = None) -> Evidence:
    return Evidence(
        source="x", selector="SITEID=1004", statistic=statistic, value=value, threshold=threshold
    )


LAG = n.narrator_input(
    _finding(
        FindingKind.KRI_BREACH,
        "1004",
        "Site 1004: data entry lag above threshold",
        "Median entry lag at site 1004 is 14.0 days over 325 visits; the threshold is 7 days.",
        [
            _ev("data_entry_lag:median_days", 14.0, 7.0),
            _ev("data_entry_lag:entries", 325),
            ratio_evidence("ops/data_entry", "SITEID=1004", "data_entry_lag", 14.0, 7.0),
        ],
    )
)
LAG_NO_RATIO = n.narrator_input(
    _finding(
        FindingKind.KRI_BREACH,
        "1004",
        "Site 1004: data entry lag above threshold",
        "Median entry lag at site 1004 is 14.0 days over 325 visits; the threshold is 7 days.",
        [_ev("data_entry_lag:median_days", 14.0, 7.0), _ev("data_entry_lag:entries", 325)],
    )
)
AE = n.narrator_input(
    _finding(
        FindingKind.UNDER_REPORTING,
        "1005",
        "Site 1005: fewer adverse events than expected",
        "Site 1005 has 27 adverse events over 2075 subject-days; the rest of the study implies "
        "70.2. With dispersion phi = 1.27, z = -5.10, p = 3.3e-09, 4.3e-08 after Bonferroni "
        "over 13 sites.",
        [
            _ev("ae_rate:observed", 27),
            _ev("ae_rate:expected", 70.22649957840258),
            _ev("ae_rate:phi", 1.2738),
            _ev("ae_rate:z", -5.1),
            _ev("ae_rate:p", 3.345754311058903e-09),
            _ev("ae_rate:p_adjusted", 4.349480604376574e-08, 0.01),
            _ev("ae_rate:per_100_subject_days", 1.3012048192771084, 3.384409618236269),
        ],
    )
)
QTL = n.narrator_input(
    _finding(
        FindingKind.QTL_BREACH,
        None,
        "Premature discontinuation approaching its QTL",
        "144 of 254 treated subjects (56.7%) discontinued before completion; the QTL is 61.7% "
        "and approaching starts at 59.7%.",
        [
            _ev("premature_discontinuation:rate", 0.5669291338582677, 0.617),
            _ev("premature_discontinuation:discontinued", 144),
            _ev("premature_discontinuation:treated", 254),
            _ev("premature_discontinuation:approach_from", 0.597),
            share_of_limit_evidence("blinded/ds", "x", 0.5669291338582677, 0.617),
        ],
    )
)
CSM = n.narrator_input(
    _finding(
        FindingKind.SITE_DATA_ANOMALY,
        "1001",
        "Site 1001: vital signs vary unusually little",
        "Site 1001 has a much smaller spread than the other sites for SYSBP (robust z of log SD "
        "below -3.5 and SD below 0.6 of the median site). Terminal digits are attached as "
        "supporting evidence only.",
        [
            _ev("sysbp:log_sd_z", -5.459, -3.5),
            _ev("sysbp:sd_ratio", 0.4508, 0.6),
            _ev("sysbp:terminal_zero_share", 0.3726),
            _ev("sysbp:terminal_digit_chi2_p", 0.0, 0.01),
        ],
    )
)


def _v(inp: dict[str, Any], headline: str, explanation: str) -> list[str]:
    reply = json.dumps({"headline": headline, "explanation": explanation})
    return n.reason_codes(n.validate(reply, inp).reasons)


def test_the_input_carries_displays_roles_and_directions_not_values() -> None:
    for e in AE["evidence"]:
        assert set(e) == n.ALLOWED_EVIDENCE_FIELDS
        assert e["display"]
    assert {e["role"] for e in AE["evidence"]} == {"primary", "supporting"}
    assert n.deid_problems(canonical_json(AE)) == []
    assert n.deid_problems(canonical_json(AE | {"cut": "x"})) == ["field_not_allowed:cut"]


def test_the_prompt_names_the_new_rules() -> None:
    assert "display" in n.SYSTEM_PROMPT
    assert "supporting" in n.SYSTEM_PROMPT
    assert "below 1e-12" in n.SYSTEM_PROMPT
    assert n.EM_DASH not in n.SYSTEM_PROMPT


def test_good_examples_pass() -> None:
    assert (
        _v(
            AE,
            "Site 1005: fewer adverse events than expected",
            "Site 1005 recorded 27 adverse events against 70.2 expected from the rest of the "
            "study, with Bonferroni-adjusted p = 4.3e-08 over 13 sites.",
        )
        == []
    )
    assert (
        _v(
            LAG,
            "Site 1004: data entry lag above threshold",
            "Site 1004 has a median entry lag of 14.0 days across 325 visits, 2.00 times the "
            "7-day threshold.",
        )
        == []
    )
    assert (
        _v(
            QTL,
            "Premature discontinuation approaching its QTL",
            "144 of 254 treated subjects (56.7%) discontinued, below the 61.7% QTL and at "
            "91.9% of the QTL.",
        )
        == []
    )
    assert (
        _v(
            CSM,
            "Site 1001: vital signs vary unusually little",
            "Site 1001 has an SD 0.45 times the median site for SYSBP. As supporting context, "
            "37.3% of readings end in 0, against 10.0% by chance.",
        )
        == []
    )


# number_not_displayed


def test_a_number_that_matches_a_display_passes_and_a_rounded_one_fails() -> None:
    ok = "Site 1005 recorded 27 adverse events against 70.2 expected."
    assert _v(AE, "Site 1005: fewer adverse events", ok) == []
    rounded = "Site 1005 recorded 27 adverse events against about 70 expected."
    assert _v(AE, "Site 1005: fewer adverse events", rounded) == ["number_not_displayed"]


def test_thousands_separators_and_integer_counts_are_accepted() -> None:
    assert (
        _v(
            AE,
            "Site 1005: fewer adverse events",
            "Site 1005 had 27 adverse events over 2,075 subject-days.",
        )
        == []
    )


def test_a_summary_number_at_another_precision_is_not_a_display() -> None:
    text = "Site 1005 recorded 27 adverse events, adjusted p = 4.35e-08."
    assert _v(AE, "Site 1005: fewer adverse events", text) == ["number_not_displayed"]


# unbacked_ratio


def test_a_ratio_that_exists_as_evidence_passes() -> None:
    assert (
        _v(
            LAG,
            "Site 1004: data entry lag above threshold",
            "Site 1004's median entry lag of 14.0 days is twice the 7-day threshold.",
        )
        == []
    )


@pytest.mark.parametrize("phrase", ["twice", "3 times", "a 2-fold", "three times", "half"])
def test_a_ratio_without_evidence_is_rejected(phrase: str) -> None:
    text = f"Site 1004's median entry lag of 14.0 days is {phrase} the 7-day threshold."
    assert "unbacked_ratio" in _v(LAG_NO_RATIO, "Site 1004: data entry lag", text)


# supporting_as_headline


def test_a_supporting_statistic_in_the_headline_is_rejected() -> None:
    expl = "Site 1001 has an SD 0.45 times the median site for SYSBP."
    assert _v(CSM, "Site 1001: vital signs vary unusually little", expl) == []
    assert _v(CSM, "Site 1001: 37.3% of readings end in 0", expl) == ["supporting_as_headline"]
    assert _v(CSM, "Site 1001: too many zeros", expl) == ["supporting_as_headline"]


# zero_p_value


@pytest.mark.parametrize("p", ["p = 0", "p values of 0", "a p of 0", "p-value of 0.0"])
def test_a_zero_p_value_is_rejected(p: str) -> None:
    text = f"Site 1001 has an SD 0.45 times the median site, with terminal-digit {p}."
    assert "zero_p_value" in _v(CSM, "Site 1001: vital signs vary unusually little", text)


def test_a_floored_p_value_passes() -> None:
    text = "Site 1001 has an SD 0.45 times the median site; terminal-digit test p below 1e-12."
    assert _v(CSM, "Site 1001: vital signs vary unusually little", text) == []
    assert "zero_p_value" not in _v(AE, "Site 1005", "Site 1005 has p = 0.01 as threshold.")


# misattribution


def test_citing_the_summary_or_title_is_rejected() -> None:
    good = "Site 1005 recorded 27 adverse events against 70.2 expected."
    assert _v(AE, "Site 1005: fewer adverse events", good) == []
    for bad in (
        "The summary notes that Site 1005 recorded 27 adverse events.",
        "As the title says, Site 1005 recorded 27 adverse events.",
        "According to the summary, Site 1005 recorded 27 adverse events.",
    ):
        assert _v(AE, "Site 1005: fewer adverse events", bad) == ["misattribution"]


# threshold_restated


def test_a_share_of_the_limit_must_match_its_display() -> None:
    good = "144 of 254 treated subjects discontinued, 91.9% of the QTL."
    assert _v(QTL, "Premature discontinuation approaching its QTL", good) == []
    bad = "144 of 254 treated subjects discontinued, having reached 90% of the limit."
    # 90% is also not a display of this (2.1.0) finding; a 1.0.0 finding displayed it.
    assert "threshold_restated" in _v(QTL, "Premature discontinuation approaching its QTL", bad)


# direction_contradicted


def test_a_flipped_direction_is_rejected() -> None:
    good = "Site 1005 recorded fewer adverse events than expected: 27 against 70.2."
    assert _v(AE, "Site 1005: fewer adverse events than expected", good) == []
    bad = "Site 1005 recorded more adverse events than expected: 27 against 70.2."
    assert _v(AE, "Site 1005: more adverse events than expected", bad) == ["direction_contradicted"]


def test_a_p_value_below_its_threshold_is_not_a_direction_contradiction() -> None:
    text = "Site 1004 has a median entry lag of 14.0 days, above the 7-day threshold."
    assert _v(LAG, "Site 1004: data entry lag above threshold", text) == []
    assert (
        _v(
            AE,
            "Site 1005: fewer adverse events",
            "Site 1005 recorded 27 adverse events, with Bonferroni-adjusted p = 4.3e-08 "
            "below the 0.01 threshold.",
        )
        == []
    )
    flipped = "Site 1005 recorded 27 adverse events, with adjusted p = 4.3e-08 above 0.01."
    assert _v(AE, "Site 1005: fewer adverse events", flipped) == ["direction_contradicted"]


def test_reason_codes_strip_details() -> None:
    assert n.reason_codes(["number_not_displayed:43", "number_not_displayed:9", "em_dash"]) == [
        "number_not_displayed",
        "em_dash",
    ]


def test_a_decimal_point_does_not_end_a_clause() -> None:
    """Regression (found by the labelled set, RBQM-0041): "1.57e-06 is below" stays in the
    clause that names the p value."""
    text = "Site 1005 recorded 27 adverse events. The adjusted p = 4.3e-08 is below 0.01."
    assert _v(AE, "Site 1005: fewer adverse events", text) == []
    deviations = n.narrator_input(
        _finding(
            FindingKind.KRI_BREACH,
            "1016",
            "Site 1016: more protocol deviations than expected",
            "x",
            [
                _ev("deviations:observed", 18),
                _ev("deviations:expected", 5.0945),
                _ev("deviations:p_adjusted", 1.571e-06, 0.01),
            ],
        )
    )
    text = (
        "Site 1016 recorded 18 deviations. The adjusted p of 1.6e-06 is below the 0.01 threshold."
    )
    assert _v(deviations, "Site 1016: more protocol deviations than expected", text) == []
