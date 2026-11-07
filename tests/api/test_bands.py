"""RBQM heatmap bands: each column follows what its agent tests (direction, threshold)."""

import pytest
from fastapi.testclient import TestClient

from meridian.api.models import ColumnRule
from meridian.api.sites import band, column_rules
from meridian.platform.config import Settings

AE = ColumnRule(
    agent="a", unit="z", direction="low", warning_from=2.0, critical_beyond=-3.167, rule=""
)
DEV = ColumnRule(
    agent="a", unit="z", direction="high", warning_from=2.0, critical_beyond=3.167, rule=""
)
SPREAD = ColumnRule(
    agent="c", unit="z", direction="low", warning_from=2.0, critical_beyond=-3.5, rule=""
)
LAG = ColumnRule(
    agent="a", unit="days", direction="high", warning_from=5.25, critical_beyond=7.0, rule=""
)


@pytest.mark.parametrize(
    ("rule", "value", "expected"),
    [
        (AE, -1.99, "normal"),
        (AE, -2.0, "warning"),
        (AE, -3.167, "warning"),  # the agent flags strictly beyond its threshold
        (AE, -3.17, "critical"),
        (AE, 2.5, "untested_direction"),  # more AEs than expected: not what ae_rate tests
        (AE, 9.0, "untested_direction"),
        (DEV, 3.0, "warning"),
        (DEV, 3.2, "critical"),
        (DEV, -4.0, "untested_direction"),
        (SPREAD, -3.5, "warning"),
        (SPREAD, -3.51, "critical"),
        (SPREAD, 4.0, "untested_direction"),  # csm only flags low spread
        (SPREAD, 0.0, "normal"),
        (LAG, 5.25, "normal"),
        (LAG, 5.3, "warning"),
        (LAG, 7.0, "warning"),
        (LAG, 7.01, "critical"),
        (LAG, None, "na"),
    ],
)
def test_band_boundaries(rule: ColumnRule, value: float | None, expected: str) -> None:
    assert band(value, rule) == expected


def test_column_rules_come_from_the_agents_config() -> None:
    agents = Settings().agents
    rules = column_rules(agents, n_ae=13, n_dev=13, parameters=("SYSBP", "DIABP", "PULSE"))
    # kri_monitor flags when min(1, p * 13) < 0.01, p = Phi(z): z below Phi^-1(0.01 / 13).
    assert rules["ae_rate_z"].critical_beyond == pytest.approx(-3.1683, abs=1e-3)
    assert rules["deviation_z"].critical_beyond == pytest.approx(3.1683, abs=1e-3)
    assert rules["median_entry_lag_days"].critical_beyond == agents.kri_monitor.lag_days
    assert rules["median_open_query_age_days"].critical_beyond == agents.kri_monitor.query_age_days
    assert rules["csm_log_sd_z:PULSE"].critical_beyond == -agents.csm.z_threshold
    assert {r.direction for k, r in rules.items() if k.startswith("csm")} == {"low"}
    fewer = column_rules(agents, n_ae=4, n_dev=4, parameters=())
    assert abs(fewer["ae_rate_z"].critical_beyond) < abs(rules["ae_rate_z"].critical_beyond)


CHECK = {
    "ae_rate_z": "ae_rate",
    "deviation_z": "deviations",
    "median_entry_lag_days": "data_entry_lag",
    "median_open_query_age_days": "query_aging",
}


def test_no_critical_cell_without_an_outline_on_real_cuts() -> None:
    """On DC-BASE and every evaluation cut with planted faults (faulted, holdout, sweep),
    every critical cell is covered by a current finding. Skipped without local data."""
    from meridian.cli import _api
    from meridian.platform.clock import SystemClock

    s = Settings()
    if not s.ledger_path.exists() or not (s.curated_root / s.study_id / "DC-BASE").exists():
        pytest.skip("no local ledger or cut data")
    client = TestClient(_api(s, SystemClock()))
    qa = {"X-Meridian-Actor": "nina.fernandes", "X-Meridian-Role": "qa_auditor"}
    cuts = [
        c["cut_id"]
        for c in client.get(f"/api/v1/studies/{s.study_id}/cuts", headers=qa).json()
        if c["kind"] in ("base", "faulted", "holdout", "sweep")
    ]
    assert "DC-BASE" in cuts
    assert len(cuts) > 1
    uncovered, critical = [], 0
    for cut in cuts:
        sites = client.get(f"/api/v1/rbqm/{cut}/sites", headers=qa).json()["sites"]
        for site in sites:
            for key, b in site["bands"].items():
                if b != "critical":
                    continue
                critical += 1
                check = "variance" if key.startswith("csm") else CHECK[key]
                param = key.split(":", 1)[1] if ":" in key else None
                if not any(
                    f["check"] == check
                    and (param is None or not f["parameters"] or param in f["parameters"])
                    for f in site["flagged_by"]
                ):
                    uncovered.append(f"{cut} {site['site_id']} {key}")
    assert critical > 0
    assert uncovered == []


AGE = ColumnRule(
    agent="a",
    unit="days",
    direction="high",
    warning_from=15.75,
    critical_beyond=21.0,
    rule="",
    min_count=5,
    count_label="open queries",
)


@pytest.mark.parametrize(
    ("value", "count", "expected"),
    [
        (15.8, 4, "too_few"),  # site 1002: a warning value, but too few queries to test
        (40.0, 3, "too_few"),
        (40.0, None, "too_few"),
        (15.8, 5, "warning"),
        (21.5, 5, "critical"),
        (10.0, 9, "normal"),
        (None, 9, "na"),
    ],
)
def test_the_minimum_open_query_count_applies_to_every_band(
    value: float | None, count: int | None, expected: str
) -> None:
    assert band(value, AGE, count) == expected


def test_only_query_age_has_a_minimum_count() -> None:
    agents = Settings().agents
    rules = column_rules(agents, n_ae=13, n_dev=13, parameters=("SYSBP",))
    age = rules["median_open_query_age_days"]
    assert (age.min_count, age.count_label) == (agents.kri_monitor.min_open_queries, "open queries")
    assert [k for k, r in rules.items() if r.min_count is not None] == [
        "median_open_query_age_days"
    ]
