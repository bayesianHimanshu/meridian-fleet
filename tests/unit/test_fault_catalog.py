import math

import numpy as np
import polars as pl
import pytest

from meridian.data.derived import premature_discontinuation, treated_subjects
from meridian.faults.catalog import REGISTRY, FaultError, FaultRecord, FaultSpec, Tables
from tests.rbqm_support import make_tables

TARGET = "1002"


@pytest.fixture(scope="module")
def tables() -> Tables:
    blinded, ops, _ = make_tables()
    return {**blinded, **ops}


def _apply(
    tables: Tables, fault_type: str, target: str, params: dict[str, float], seed: int = 1
) -> tuple[Tables, FaultRecord]:
    spec = FaultSpec(fault_id="F-001", fault_type=fault_type, target=target, params=params)
    return REGISTRY[fault_type].fn(dict(tables), spec, np.random.default_rng(seed))


def _site_of(tables: Tables) -> dict[str, str]:
    return dict(tables["dm"].select("USUBJID", "SITEID").iter_rows())


def _other_site_rows(frame: pl.DataFrame, tables: Tables, target: str) -> pl.DataFrame:
    if "SITEID" in frame.columns:
        return frame.filter(pl.col("SITEID") != target)
    others = [u for u, s in _site_of(tables).items() if s != target]
    return frame.filter(pl.col("USUBJID").is_in(others))


SITE_FAULTS = {
    "fabricated_vitals": (
        {"subject_share": 0.8, "sd_scale": 0.25, "terminal_zero_share": 0.4},
        "vs",
    ),
    "ae_under_reporting": ({"drop_share": 0.7}, "ae"),
    "entry_lag_spike": ({"target_median_days": 14.0}, "data_entry"),
    "query_backlog": ({"target_median_age_days": 40.0}, "queries"),
    "deviation_cluster": ({"rate_multiplier": 4.0}, "deviations"),
}


@pytest.mark.parametrize("fault_type", sorted(SITE_FAULTS))
def test_site_fault_is_deterministic_and_touches_only_the_target(
    tables: Tables, fault_type: str
) -> None:
    params, table = SITE_FAULTS[fault_type]
    a, rec_a = _apply(tables, fault_type, TARGET, params, seed=5)
    b, rec_b = _apply(tables, fault_type, TARGET, params, seed=5)
    assert rec_a == rec_b
    for name in tables:
        assert a[name].equals(b[name], null_equal=True), name
        if name != table:
            assert a[name].equals(tables[name], null_equal=True), f"{fault_type} changed {name}"
    assert _other_site_rows(a[table], tables, TARGET).equals(
        _other_site_rows(tables[table], tables, TARGET), null_equal=True
    )
    assert not a[table].equals(tables[table], null_equal=True)


@pytest.mark.parametrize(
    "fault_type", ["fabricated_vitals", "ae_under_reporting", "deviation_cluster"]
)
def test_rng_faults_depend_on_the_seed(tables: Tables, fault_type: str) -> None:
    params, table = SITE_FAULTS[fault_type]
    a, _ = _apply(tables, fault_type, TARGET, params, seed=1)
    b, _ = _apply(tables, fault_type, TARGET, params, seed=2)
    assert not a[table].equals(b[table], null_equal=True)


def _site_treated(tables: Tables, site: str) -> list[str]:
    return treated_subjects(tables["dm"]).filter(pl.col("SITEID") == site)["USUBJID"].to_list()


def test_fabricated_vitals_lowers_sd_and_raises_terminal_zeros(tables: Tables) -> None:
    out, _ = _apply(tables, "fabricated_vitals", TARGET, SITE_FAULTS["fabricated_vitals"][0])
    site = _site_treated(tables, TARGET)

    def stats(frame: pl.DataFrame, test: str) -> tuple[float, float]:
        v = frame.filter((pl.col("VSTESTCD") == test) & pl.col("USUBJID").is_in(site))
        values = v["VSSTRESN"].to_numpy()
        return float(np.std(values, ddof=1)), float(np.mean(values % 10 == 0))

    for test in ("SYSBP", "DIABP", "PULSE"):
        sd_before, zero_before = stats(tables["vs"], test)
        sd_after, zero_after = stats(out["vs"], test)
        assert sd_after < 0.6 * sd_before, test
        assert zero_after > zero_before + 0.15, test
    changed = out["vs"].filter(pl.col("VSORRES") != tables["vs"]["VSORRES"])
    assert (changed["VSORRES"] == changed["VSSTRESN"].cast(pl.Int64).cast(pl.String)).all()
    assert (out["vs"]["VSSTRESC"] == out["vs"]["VSORRES"]).all()


def test_ae_under_reporting_drops_share_and_renumbers(tables: Tables) -> None:
    out, _ = _apply(tables, "ae_under_reporting", TARGET, {"drop_share": 0.7})
    site = _site_treated(tables, TARGET)
    before = tables["ae"].filter(pl.col("USUBJID").is_in(site)).height
    after = out["ae"].filter(pl.col("USUBJID").is_in(site)).height
    assert after == before - round(0.7 * before)
    seqs = (
        out["ae"]
        .group_by("USUBJID")
        .agg((pl.col("AESEQ").sort() == pl.int_range(1, pl.len() + 1)).all().alias("ok"))
    )
    assert seqs["ok"].all()


def test_entry_lag_spike_reaches_target_median(tables: Tables) -> None:
    out, _ = _apply(tables, "entry_lag_spike", TARGET, {"target_median_days": 14.0})
    site = _site_treated(tables, TARGET)
    de = out["data_entry"].filter(pl.col("USUBJID").is_in(site))
    assert de["entry_lag_days"].median() == pytest.approx(14.0)
    expected = de.select(
        pl.col("SVSTDTC_DATE") + pl.duration(days=pl.col("entry_lag_days").ceil().cast(pl.Int64))
    ).to_series()
    assert (de["entered_date"] == expected).all()
    # The site median and its z still reproduce: ln(median) is linear in z across sites.
    medians = (
        out["data_entry"]
        .group_by("SITEID")
        .agg(pl.col("site_median_lag_days").first(), pl.col("site_median_z").first())
    )
    fit = np.polyfit(
        medians["site_median_z"].to_numpy(),
        np.log(medians["site_median_lag_days"].to_numpy()),
        1,
        full=True,
    )
    assert float(fit[1][0]) < 1e-12


def test_query_backlog_reaches_target_median(tables: Tables) -> None:
    out, _ = _apply(tables, "query_backlog", TARGET, {"target_median_age_days": 40.0})
    site = _site_treated(tables, TARGET)
    q = out["queries"].filter(pl.col("USUBJID").is_in(site) & (pl.col("status") == "open"))
    assert q.height >= 5
    assert q["age_days"].median() == pytest.approx(40.0)


def test_query_backlog_reopens_closed_queries_when_too_few_open(tables: Tables) -> None:
    site = _site_treated(tables, TARGET)
    q = tables["queries"]
    in_site = pl.col("USUBJID").is_in(site)
    open_rows = q.with_row_index("_r").filter(in_site & (pl.col("status") == "open"))["_r"]
    keep_open = set(open_rows.head(2).to_list())
    starved = (
        q.with_row_index("_r")
        .with_columns(
            pl.when(in_site & (pl.col("status") == "open") & ~pl.col("_r").is_in(list(keep_open)))
            .then(pl.lit("closed"))
            .otherwise(pl.col("status"))
            .alias("status"),
        )
        .with_columns(
            pl.when(pl.col("status") == "closed")
            .then(None)
            .otherwise(pl.col("age_days"))
            .alias("age_days"),
            pl.when(pl.col("status") == "closed")
            .then(pl.max_horizontal(pl.col("status_draw"), pl.lit(0.5)))
            .otherwise(pl.col("status_draw"))
            .alias("status_draw"),
        )
        .drop("_r")
    )
    out, record = _apply(
        {**tables, "queries": starved}, "query_backlog", TARGET, {"target_median_age_days": 40.0}
    )
    after = out["queries"].filter(in_site & (pl.col("status") == "open"))
    assert after.height == 5
    assert after["age_days"].median() == pytest.approx(40.0)
    assert record.achieved["reopened"] == 3
    max_open = out["queries"].filter(pl.col("status") == "open")["status_draw"].max()
    min_closed = out["queries"].filter(pl.col("status") == "closed")["status_draw"].min()
    assert max_open < min_closed  # type: ignore[operator]


def test_deviation_cluster_reaches_target_rate(tables: Tables) -> None:
    out, _ = _apply(tables, "deviation_cluster", TARGET, {"rate_multiplier": 4.0})
    treated = treated_subjects(tables["dm"])
    site = _site_treated(tables, TARGET)
    others = treated.filter(pl.col("SITEID") != TARGET)
    dev = tables["deviations"]
    other_rate = (
        dev.filter(pl.col("USUBJID").is_in(others["USUBJID"].to_list())).height / others.height
    )
    achieved = out["deviations"].filter(pl.col("USUBJID").is_in(site)).height / len(site)
    assert 4.0 * other_rate <= achieved < 4.0 * other_rate + 1 / len(site)
    site_rows = out["deviations"].filter(pl.col("SITEID") == TARGET)
    assert (site_rows["site_deviation_count"] == site_rows.height).all()
    assert site_rows["deviation_id"].n_unique() == site_rows.height


def test_discontinuation_drift_reaches_target_and_touches_only_ds(tables: Tables) -> None:
    before, n = premature_discontinuation(tables["dm"], tables["ds"])
    target = round(before / n + 0.1, 3)
    out, _ = _apply(tables, "discontinuation_drift", "STUDY", {"target_rate": target})
    after, _ = premature_discontinuation(out["dm"], out["ds"])
    assert target <= after / n < target + 1 / n
    for name in tables:
        if name != "ds":
            assert out[name].equals(tables[name], null_equal=True), name
    changed = out["ds"].filter(pl.col("DSDECOD") != tables["ds"]["DSDECOD"])
    assert set(changed["DSDECOD"]) == {"WITHDRAWAL BY SUBJECT"}
    assert (changed["DSTERM"] == "WITHDRAWAL BY SUBJECT").all()
    assert math.isclose(changed.height, math.ceil(target * n - 1e-9) - before)


def test_unknown_site_is_an_error(tables: Tables) -> None:
    with pytest.raises(FaultError, match="no treated"):
        _apply(tables, "ae_under_reporting", "9999", {"drop_share": 0.5})
