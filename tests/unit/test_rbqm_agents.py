from pathlib import Path

import polars as pl
import pytest
import structlog

from meridian.agents.base import parse_dedup_key
from meridian.agents.rbqm.csm import Csm, CsmConfig
from meridian.agents.rbqm.kri_monitor import KriMonitor, KriMonitorConfig
from meridian.agents.rbqm.qtl_detector import (
    QtlDetector,
    QtlDetectorConfig,
    QtlStatus,
    qtl_status,
)
from meridian.data.derived import premature_discontinuation
from meridian.domain.enums import FindingKind, Severity
from meridian.stats.site_metrics import sites_with_min_treated
from tests.agent_support import clean, plant, run_agent
from tests.rbqm_support import make_tables

TARGET = "1003"


def _keys(drafts: list) -> list[tuple[str, str, str]]:  # type: ignore[type-arg]
    return [
        (d.kind.value, d.site_id or "STUDY", parse_dedup_key(d.dedup_key).check) for d in drafts
    ]


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
    small = "1012"
    blinded, ops = plant(blinded, ops, "ae_under_reporting", small, {"drop_share": 1.0})
    with structlog.testing.capture_logs() as logs:
        drafts = run_agent(KriMonitor(), KriMonitorConfig(), tmp_path, "F", blinded, ops, unblinded)
    assert all(d.site_id != small for d in drafts)
    skipped = [e for e in logs if e["event"] == "kri_monitor.sites_skipped"]
    assert skipped
    assert skipped[0]["sites"] == [small]


def test_agent_versions() -> None:
    assert KriMonitor().version == "2.1.0"
    for agent in (QtlDetector(), Csm()):
        assert agent.version == "2.0.0"


def test_kri_rate_evidence_carries_phi_and_z(tmp_path: Path) -> None:
    blinded, ops, unblinded = clean()
    blinded, ops = plant(blinded, ops, "ae_under_reporting", TARGET, {"drop_share": 0.7})
    (draft,) = run_agent(KriMonitor(), KriMonitorConfig(), tmp_path, "F", blinded, ops, unblinded)
    statistics = {e.statistic: e.value for e in draft.evidence}
    assert {"ae_rate:phi", "ae_rate:z", "ae_rate:expected", "ae_rate:observed"} <= set(statistics)
    assert statistics["ae_rate:phi"] >= 1.0
    assert statistics["ae_rate:z"] < 0
    assert parse_dedup_key(draft.dedup_key).agent_version == "2.1.0"


# qtl_detector


def test_qtl_status_bands() -> None:
    assert qtl_status(0.57, 0.617, 0.02) is QtlStatus.WITHIN
    assert qtl_status(0.597, 0.617, 0.02) is QtlStatus.APPROACHING
    assert qtl_status(0.616, 0.617, 0.02) is QtlStatus.APPROACHING
    assert qtl_status(0.617, 0.617, 0.02) is QtlStatus.BREACHED
    assert qtl_status(0.6, 0.617, 0.0) is QtlStatus.WITHIN


def _rate(blinded: dict) -> float:  # type: ignore[type-arg]
    d, n = premature_discontinuation(blinded["dm"], blinded["ds"])
    return d / n


def test_qtl_base_just_below_limit_is_within_not_approaching(tmp_path: Path) -> None:
    tables = clean()
    rate = _rate(tables[0])
    # The step 3 failure mode: limit = base + 0.05 made the base "approaching" at 90%.
    config = QtlDetectorConfig(discontinuation_limit=rate + 0.05, approach_band=0.02)
    assert run_agent(QtlDetector(), config, tmp_path, "C", *tables) == []


def test_qtl_approaching_inside_the_band_is_medium(tmp_path: Path) -> None:
    tables = clean()
    config = QtlDetectorConfig(discontinuation_limit=_rate(tables[0]) + 0.01, approach_band=0.02)
    (draft,) = run_agent(QtlDetector(), config, tmp_path, "C", *tables)
    assert draft.severity is Severity.MEDIUM
    assert draft.kind is FindingKind.QTL_BREACH
    assert draft.site_id is None


def test_qtl_detector_flags_planted_drift_as_high(tmp_path: Path) -> None:
    blinded, ops, unblinded = clean()
    base = _rate(blinded)
    blinded, ops = plant(
        blinded, ops, "discontinuation_drift", "STUDY", {"target_rate": base + 0.1}
    )
    config = QtlDetectorConfig(discontinuation_limit=round(base + 0.05, 3))
    (draft,) = run_agent(QtlDetector(), config, tmp_path, "F", blinded, ops, unblinded)
    assert draft.severity is Severity.HIGH
    assert parse_dedup_key(draft.dedup_key).site == "STUDY"


# csm v2: twelve sites so the between-site robust z has something to work with

FABRICATE = {"subject_share": 0.8, "sd_scale": 0.25, "terminal_zero_share": 0.4}


def twelve() -> tuple[dict, dict, dict]:  # type: ignore[type-arg]
    return make_tables(n_sites=12, per_site=15, seed=7)


def test_csm_silent_on_clean_twelve_sites(tmp_path: Path) -> None:
    assert run_agent(Csm(), CsmConfig(), tmp_path, "C", *twelve()) == []


def test_csm_flags_one_fabricated_site_among_twelve(tmp_path: Path) -> None:
    blinded, ops, unblinded = twelve()
    blinded, ops = plant(blinded, ops, "fabricated_vitals", "1005", FABRICATE)
    drafts = run_agent(Csm(), CsmConfig(), tmp_path, "F", blinded, ops, unblinded)
    assert _keys(drafts) == [("site_data_anomaly", "1005", "variance")]
    (draft,) = drafts
    assert draft.severity is Severity.HIGH  # all three parameters flag
    stats = {e.statistic for e in draft.evidence}
    assert {"sysbp:log_sd_z", "sysbp:sd_ratio", "sysbp:terminal_zero_share"} <= stats
    assert draft.score > CsmConfig().z_threshold


def test_csm_flags_two_fabricated_sites_without_masking(tmp_path: Path) -> None:
    blinded, ops, unblinded = twelve()
    for site in ("1003", "1009"):
        blinded, ops = plant(blinded, ops, "fabricated_vitals", site, FABRICATE)
    drafts = run_agent(Csm(), CsmConfig(), tmp_path, "F", blinded, ops, unblinded)
    assert [d.site_id for d in drafts] == ["1003", "1009"]


def test_csm_digit_preference_alone_raises_nothing(tmp_path: Path) -> None:
    blinded, ops, unblinded = twelve()
    site_subjects = blinded["dm"].filter(pl.col("SITEID") == "1004")["USUBJID"].to_list()
    rounded = pl.col("VSSTRESN").truediv(10).round().mul(10)
    vs = (
        blinded["vs"]
        .with_row_index("_r")
        .with_columns(
            pl.when(pl.col("USUBJID").is_in(site_subjects) & (pl.col("_r") % 3 != 0))
            .then(rounded)
            .otherwise(pl.col("VSSTRESN"))
            .alias("VSSTRESN")
        )
        .drop("_r")
    )
    zero = vs.filter(pl.col("USUBJID").is_in(site_subjects))["VSSTRESN"] % 10 == 0
    assert float(zero.mean() or 0) > 0.6  # strong preference for 0, spread unchanged
    blinded = {**blinded, "vs": vs}
    assert run_agent(Csm(), CsmConfig(), tmp_path, "D", blinded, ops, unblinded) == []


def test_csm_skips_small_sites(tmp_path: Path) -> None:
    blinded, ops, unblinded = make_tables(n_sites=12, per_site=15, seed=7, small_site=3)
    small = "1012"
    assert small not in sites_with_min_treated(blinded["dm"], 5)[0]
    blinded, ops = plant(
        blinded,
        ops,
        "fabricated_vitals",
        small,
        {"subject_share": 1.0, "sd_scale": 0.1, "terminal_zero_share": 0.9},
    )
    assert run_agent(Csm(), CsmConfig(), tmp_path, "F", blinded, ops, unblinded) == []


def test_csm_is_deterministic(tmp_path: Path) -> None:
    blinded, ops, unblinded = twelve()
    for site in ("1006", "1002"):
        blinded, ops = plant(blinded, ops, "fabricated_vitals", site, FABRICATE)
    a = run_agent(Csm(), CsmConfig(), tmp_path / "a", "F", blinded, ops, unblinded)
    b = run_agent(Csm(), CsmConfig(), tmp_path / "b", "F", blinded, ops, unblinded)
    assert a == b
    assert [d.site_id for d in a] == ["1002", "1006"]


@pytest.mark.parametrize("variant", ["a", "b", "c", "d", "e"])
def test_every_ae_variant_runs_and_finds_the_planted_drop(tmp_path: Path, variant: str) -> None:
    blinded, ops, unblinded = clean()
    # Fixture AE onsets spread over 120 days; a 150-day window keeps variant c comparable.
    cfg = KriMonitorConfig(ae_variant=variant, early_window_days=150)  # type: ignore[arg-type]
    assert [
        d
        for d in run_agent(KriMonitor(), cfg, tmp_path, "C", blinded, ops, unblinded)
        if parse_dedup_key(d.dedup_key).check == "ae_rate"
    ] == []
    blinded, ops = plant(blinded, ops, "ae_under_reporting", TARGET, {"drop_share": 0.7})
    drafts = run_agent(KriMonitor(), cfg, tmp_path / "f", "F", blinded, ops, unblinded)
    (draft,) = [d for d in drafts if parse_dedup_key(d.dedup_key).check == "ae_rate"]
    assert draft.site_id == TARGET
    assert f"variant {variant}" in draft.summary
