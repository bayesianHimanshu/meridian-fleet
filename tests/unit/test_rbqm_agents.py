from pathlib import Path

import pytest
import structlog

from meridian.agents.base import parse_dedup_key
from meridian.agents.rbqm.common import loo_poisson
from meridian.agents.rbqm.csm import Csm, CsmConfig
from meridian.agents.rbqm.kri_monitor import KriMonitor, KriMonitorConfig
from meridian.agents.rbqm.qtl_detector import QtlDetector, QtlDetectorConfig, QtlStatus, qtl_status
from meridian.data.derived import premature_discontinuation
from meridian.domain.enums import FindingKind, Severity
from tests.agent_support import clean, plant, run_agent

TARGET = "1003"


def _keys(drafts: list) -> list[tuple[str, str, str]]:  # type: ignore[type-arg]
    return [
        (d.kind.value, d.site_id or "STUDY", parse_dedup_key(d.dedup_key).metric) for d in drafts
    ]


# Leave-one-out


def test_leave_one_out_excludes_the_site_under_test() -> None:
    counts = {"A": 100, "B": 10, "C": 10}
    days = {"A": 10, "B": 10, "C": 10}
    results = {r.site: r for r in loo_poisson(counts, days, ["A", "B"], lower_tail=False)}
    assert results["A"].rate_others == pytest.approx(1.0)  # (10 + 10) / (10 + 10)
    assert results["A"].expected == pytest.approx(10.0)
    assert results["B"].rate_others == pytest.approx(110 / 20)
    assert results["A"].p < 1e-30
    assert "C" not in results


def test_leave_one_out_lower_tail() -> None:
    (low,) = loo_poisson(
        {"A": 1, "B": 50, "C": 50}, {"A": 10, "B": 10, "C": 10}, ["A"], lower_tail=True
    )
    assert low.expected == pytest.approx(50.0)
    assert low.p < 1e-15


# kri_monitor


def test_kri_monitor_silent_on_clean_fixture(tmp_path: Path) -> None:
    assert run_agent(KriMonitor(), KriMonitorConfig(), tmp_path, "C", *clean()) == []


@pytest.mark.parametrize(
    ("fault_type", "params", "expected"),
    [
        ("ae_under_reporting", {"drop_share": 0.7}, ("under_reporting", "ae_rate")),
        ("entry_lag_spike", {"target_median_days": 14.0}, ("kri_breach", "data_entry_lag")),
        ("query_backlog", {"target_median_age_days": 40.0}, ("kri_breach", "query_aging")),
        ("deviation_cluster", {"rate_multiplier": 4.0}, ("kri_breach", "deviations")),
    ],
)
def test_kri_monitor_flags_planted_pattern(
    tmp_path: Path, fault_type: str, params: dict[str, float], expected: tuple[str, str]
) -> None:
    blinded, ops, unblinded = clean()
    blinded, ops = plant(blinded, ops, fault_type, TARGET, params)
    drafts = run_agent(KriMonitor(), KriMonitorConfig(), tmp_path, "F", blinded, ops, unblinded)
    assert _keys(drafts) == [(expected[0], TARGET, expected[1])]
    (draft,) = drafts
    statistics = [e.statistic for e in draft.evidence]
    assert all(s.startswith(f"{expected[1]}:") for s in statistics)
    assert draft.score > 0


def test_kri_monitor_severity_and_score_for_medians(tmp_path: Path) -> None:
    blinded, ops, unblinded = clean()
    blinded, ops = plant(blinded, ops, "entry_lag_spike", TARGET, {"target_median_days": 14.0})
    (draft,) = run_agent(KriMonitor(), KriMonitorConfig(), tmp_path, "F", blinded, ops, unblinded)
    assert draft.severity is Severity.HIGH  # 14 > 1.5 * 7
    assert draft.score == pytest.approx(2.0)


def test_kri_monitor_is_deterministic_and_ordered(tmp_path: Path) -> None:
    blinded, ops, unblinded = clean()
    for fault, site, params in (
        ("query_backlog", "1005", {"target_median_age_days": 40.0}),
        ("entry_lag_spike", "1002", {"target_median_days": 14.0}),
        ("entry_lag_spike", "1001", {"target_median_days": 14.0}),
        ("ae_under_reporting", "1004", {"drop_share": 0.8}),
    ):
        blinded, ops = plant(blinded, ops, fault, site, params)
    first = run_agent(
        KriMonitor(), KriMonitorConfig(), tmp_path / "a", "F", blinded, ops, unblinded
    )
    second = run_agent(
        KriMonitor(), KriMonitorConfig(), tmp_path / "b", "F", blinded, ops, unblinded
    )
    assert first == second
    assert _keys(first) == [
        ("under_reporting", "1004", "ae_rate"),
        ("kri_breach", "1001", "data_entry_lag"),
        ("kri_breach", "1002", "data_entry_lag"),
        ("kri_breach", "1005", "query_aging"),
    ]


def test_kri_monitor_skips_small_sites_for_rate_tests(tmp_path: Path) -> None:
    blinded, ops, unblinded = clean(small_site=3)
    small = "1006"
    blinded, ops = plant(blinded, ops, "ae_under_reporting", small, {"drop_share": 1.0})
    with structlog.testing.capture_logs() as logs:
        drafts = run_agent(KriMonitor(), KriMonitorConfig(), tmp_path, "F", blinded, ops, unblinded)
    assert all(d.site_id != small for d in drafts)
    skipped = [e for e in logs if e["event"] == "kri_monitor.sites_skipped"]
    assert skipped
    assert skipped[0]["sites"] == [small]


# qtl_detector


def test_qtl_status_bands() -> None:
    assert qtl_status(0.5, 0.6, 0.9) is QtlStatus.WITHIN
    assert qtl_status(0.54, 0.6, 0.9) is QtlStatus.APPROACHING
    assert qtl_status(0.6, 0.6, 0.9) is QtlStatus.BREACHED


def _rate(tables: tuple[dict, dict, dict]) -> float:  # type: ignore[type-arg]
    d, n = premature_discontinuation(tables[0]["dm"], tables[0]["ds"])
    return d / n


def test_qtl_detector_silent_within(tmp_path: Path) -> None:
    tables = clean()
    config = QtlDetectorConfig(discontinuation_limit=round(_rate(tables) + 0.2, 3))
    assert run_agent(QtlDetector(), config, tmp_path, "C", *tables) == []


def test_qtl_detector_approaching_is_medium(tmp_path: Path) -> None:
    tables = clean()
    config = QtlDetectorConfig(discontinuation_limit=_rate(tables) / 0.95)
    (draft,) = run_agent(QtlDetector(), config, tmp_path, "C", *tables)
    assert draft.severity is Severity.MEDIUM
    assert draft.kind is FindingKind.QTL_BREACH
    assert draft.site_id is None
    assert draft.score == pytest.approx(0.95)


def test_qtl_detector_flags_planted_drift_as_high(tmp_path: Path) -> None:
    blinded, ops, unblinded = clean()
    base = _rate((blinded, ops, unblinded))
    limit = round(base + 0.05, 3)
    blinded, ops = plant(
        blinded, ops, "discontinuation_drift", "STUDY", {"target_rate": base + 0.1}
    )
    (draft,) = run_agent(
        QtlDetector(),
        QtlDetectorConfig(discontinuation_limit=limit),
        tmp_path,
        "F",
        blinded,
        ops,
        unblinded,
    )
    assert draft.severity is Severity.HIGH
    assert parse_dedup_key(draft.dedup_key).site == "STUDY"


# csm


def test_csm_silent_on_clean_fixture(tmp_path: Path) -> None:
    assert run_agent(Csm(), CsmConfig(), tmp_path, "C", *clean()) == []


def test_csm_flags_fabricated_vitals_with_both_tests(tmp_path: Path) -> None:
    blinded, ops, unblinded = clean()
    blinded, ops = plant(
        blinded,
        ops,
        "fabricated_vitals",
        TARGET,
        {"subject_share": 0.8, "sd_scale": 0.25, "terminal_zero_share": 0.4},
    )
    drafts = run_agent(Csm(), CsmConfig(), tmp_path, "F", blinded, ops, unblinded)
    assert _keys(drafts) == [("site_data_anomaly", TARGET, "-")]
    (draft,) = drafts
    assert draft.severity is Severity.HIGH
    statistics = {e.statistic for e in draft.evidence}
    assert {"sysbp:sd_ratio", "sysbp:brown_forsythe_p", "sysbp:terminal_digit_chi2_p"} <= statistics


def test_csm_skips_small_sites(tmp_path: Path) -> None:
    blinded, ops, unblinded = clean(small_site=3)
    small = "1006"
    blinded, ops = plant(
        blinded,
        ops,
        "fabricated_vitals",
        small,
        {"subject_share": 1.0, "sd_scale": 0.1, "terminal_zero_share": 0.9},
    )
    assert run_agent(Csm(), CsmConfig(), tmp_path, "F", blinded, ops, unblinded) == []


def test_csm_is_deterministic(tmp_path: Path) -> None:
    blinded, ops, unblinded = clean()
    for site in ("1004", "1002"):
        blinded, ops = plant(
            blinded,
            ops,
            "fabricated_vitals",
            site,
            {"subject_share": 0.8, "sd_scale": 0.25, "terminal_zero_share": 0.4},
        )
    a = run_agent(Csm(), CsmConfig(), tmp_path / "a", "F", blinded, ops, unblinded)
    b = run_agent(Csm(), CsmConfig(), tmp_path / "b", "F", blinded, ops, unblinded)
    assert a == b
    sites = [d.site_id or "" for d in a]
    assert sites == sorted(sites)
    # Both fabricated sites are flagged. With two of them pooled into "others", the
    # reference itself shifts: the variance test is masked and clean sites can trip
    # the digit test (swamping). Calibration in step 4 has to live with this.
    assert {"1002", "1004"} <= set(sites)
