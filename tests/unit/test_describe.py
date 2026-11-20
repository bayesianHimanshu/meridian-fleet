"""Evidence context: display per statistic, the p floor, roles, directions, round trips."""

from pathlib import Path

import pytest

from meridian.agents.rbqm.describe import (
    describe,
    describe_one,
    fmt_p,
    ratio_evidence,
    share_of_limit_evidence,
)
from meridian.domain.enums import EventType
from meridian.domain.events import Event, FindingRaised
from meridian.domain.findings import Evidence
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from tests.support import T0, make_ledger


def _e(statistic: str, value: float, threshold: float | None = None) -> Evidence:
    return Evidence(
        source="s", selector="SITEID=1001", statistic=statistic, value=value, threshold=threshold
    )


@pytest.mark.parametrize(
    ("p", "shown"),
    [
        (0.0, "below 1e-12"),  # an underflow, never "0"
        (2.86e-268, "below 1e-12"),
        (9.99e-13, "below 1e-12"),
        (4.349480604376574e-08, "4.3e-08"),
        (0.0001683, "1.7e-04"),
        (0.001227, "0.0012"),
        (0.047, "0.047"),
        (1.0, "1"),
    ],
)
def test_p_values_have_two_significant_digits_and_a_floor(p: float, shown: str) -> None:
    assert fmt_p(p) == shown


@pytest.mark.parametrize(
    ("item", "display", "role", "direction"),
    [
        (
            _e("ae_rate:expected", 70.22649957840258),
            "70.2 expected from the rest of the study",
            "primary",
            "none",
        ),
        (
            _e("ae_rate:p_adjusted", 4.349e-08, 0.01),
            "Bonferroni-adjusted p = 4.3e-08 (threshold 0.01)",
            "primary",
            "none",
        ),
        (_e("deviations:p", 0.0), "p below 1e-12", "supporting", "none"),
        (_e("deviations:phi", 1.2738), "dispersion phi = 1.27", "supporting", "none"),
        (_e("deviations:z", 3.6229), "z = 3.62", "supporting", "high"),
        (_e("ae_rate:excluded_subjects", 0.0), "0 subjects excluded", "supporting", "none"),
        (
            _e("ae_rate:per_100_subject_days", 1.3012, 3.3844),
            "1.30 per 100 subject-days (3.38 expected)",
            "supporting",
            "low",
        ),
        (
            _e("data_entry_lag:median_days", 14.0, 7.0),
            "median entry lag 14.0 days (threshold 7 days)",
            "primary",
            "high",
        ),
        (_e("data_entry_lag:entries", 325), "325 visits", "supporting", "none"),
        (
            _e("query_aging:median_open_age_days", 40.0, 21.0),
            "median open query age 40.0 days (threshold 21 days)",
            "primary",
            "high",
        ),
        (_e("query_aging:open_queries", 43), "43 open queries", "supporting", "none"),
        (
            _e("premature_discontinuation:rate", 0.5669, 0.617),
            "56.7% discontinued (QTL 61.7%)",
            "primary",
            "low",
        ),
        (_e("premature_discontinuation:treated", 254), "254 treated", "supporting", "none"),
        (
            _e("premature_discontinuation:approach_from", 0.597),
            "approaching starts at 59.7%",
            "supporting",
            "none",
        ),
        (
            _e("premature_discontinuation:share_of_limit", 0.9188, 0.9),
            "91.9% of the QTL (approaching from 90%)",
            "supporting",
            "low",
        ),
        (
            _e("sysbp:log_sd_z", -5.459, -3.5),
            "robust z of log SD = -5.46 (threshold -3.5)",
            "primary",
            "low",
        ),
        (
            _e("pulse:sd_ratio", 0.5021, 0.6),
            "SD 0.50 times the median site (threshold 0.6)",
            "primary",
            "low",
        ),
        (
            _e("sysbp:terminal_zero_share", 0.1245),
            "12.4% of readings end in 0 (10.0% by chance)",
            "supporting",
            "high",
        ),
        (
            _e("pulse:terminal_zero_z", 3.325, 3.5),
            "z of the terminal-zero share = 3.33 (threshold 3.5)",
            "supporting",
            "high",
        ),
        (
            _e("sysbp:terminal_digit_chi2_p", 0.0, 0.01),
            "terminal-digit test p below 1e-12 (threshold 0.01)",
            "primary",
            "none",
        ),
    ],
)
def test_display_role_and_direction_per_statistic(
    item: Evidence, display: str, role: str, direction: str
) -> None:
    described = describe_one(item)
    assert (described.display, described.role, described.direction) == (display, role, direction)
    assert (described.value, described.threshold, described.statistic) == (
        item.value,
        item.threshold,
        item.statistic,
    )


def test_an_observed_count_is_judged_against_its_expected_count() -> None:
    observed, expected = describe([_e("ae_rate:observed", 27), _e("ae_rate:expected", 70.226)])
    assert observed.display == "27 observed (70.2 expected)"
    assert (observed.expected, observed.direction) == (70.226, "low")
    assert expected.role == "primary"


def test_the_terminal_zero_share_is_judged_against_chance() -> None:
    item = describe_one(_e("diabp:terminal_zero_share", 0.9164))
    assert item.expected == 0.1
    assert item.role == "supporting"


def test_computed_ratios_are_their_own_supporting_items() -> None:
    lag = describe_one(ratio_evidence("ops/data_entry", "SITEID=1004", "data_entry_lag", 14.0, 7.0))
    assert (lag.statistic, lag.value, lag.display) == (
        "data_entry_lag:ratio_to_threshold",
        2.0,
        "2.00 times the threshold",
    )
    assert lag.role == "supporting"
    share = describe_one(share_of_limit_evidence("blinded/ds", "x", 0.5669, 0.617))
    assert share.display == "91.9% of the QTL"


def test_an_unknown_statistic_is_left_as_it_is() -> None:
    item = _e("something:new", 1.5)
    assert describe_one(item) == item


def test_described_evidence_round_trips() -> None:
    item = describe_one(_e("pulse:sd_ratio", 0.5021, 0.6))
    assert Evidence.model_validate_json(item.model_dump_json()) == item


def test_a_legacy_finding_without_context_verifies_and_reads_back(tmp_path: Path) -> None:
    """Old FindingRaised payloads carry no role/direction/expected/display."""
    import json

    from tests.support import data_cut_draft, make_finding
    from tests.unit.test_legacy_ledger import _insert_legacy

    path = tmp_path / "ledger.sqlite3"
    with make_ledger(path, FixedClock(T0)) as ledger:
        cut = ledger.append(data_cut_draft(1))
    finding = json.loads(make_finding().model_dump_json())
    for e in finding["evidence"]:
        for key in ("role", "direction", "expected", "display"):
            e.pop(key)
    finding["finding_id"] = "RBQM-0001"
    old = {"type": "finding_raised", "finding": finding, "dedup_key": "rbqm.kri_sentinel|x|y|z|w|v"}
    _insert_legacy(path, 2, cut.hash, old)
    with make_ledger(path, FixedClock(T0)) as ledger:
        assert verify(ledger).ok
        (legacy,) = ledger.read(event_types=[EventType.FINDING_RAISED])
        assert isinstance(legacy, Event)
        assert isinstance(legacy.payload, FindingRaised)
        assert all(e.display is None and e.role is None for e in legacy.payload.finding.evidence)
