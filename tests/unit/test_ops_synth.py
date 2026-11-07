import polars as pl
import pytest

from meridian.data.cdisc_pilot import curate
from meridian.data.ops_synth import OPS_TABLES, OpsData, OpsSynthParams, synthesize
from tests.data_support import SITE_MAP, pilot_frames

PARAMS = OpsSynthParams()


@pytest.fixture(scope="module")
def blinded() -> dict[str, pl.DataFrame]:
    return dict(curate(pilot_frames(), SITE_MAP, "MRD-201").blinded)


def _run(b: dict[str, pl.DataFrame], seed: int, order: tuple[str, ...] = OPS_TABLES) -> OpsData:
    return synthesize(b["dm"], b["sv"], seed, PARAMS, spawn_order=order)


def test_same_seed_gives_identical_frames(blinded: dict[str, pl.DataFrame]) -> None:
    a, b = _run(blinded, 42), _run(blinded, 42)
    for name in OPS_TABLES:
        assert a.tables[name].equals(b.tables[name], null_equal=True), name


def test_different_seed_gives_different_frames(blinded: dict[str, pl.DataFrame]) -> None:
    a, b = _run(blinded, 42), _run(blinded, 43)
    assert not a.tables["data_entry"].equals(b.tables["data_entry"])
    assert any(not a.tables[n].equals(b.tables[n], null_equal=True) for n in OPS_TABLES)


def test_adding_a_spawned_child_does_not_change_existing_tables(
    blinded: dict[str, pl.DataFrame],
) -> None:
    base = _run(blinded, 42)
    extended = _run(blinded, 42, (*OPS_TABLES, "future_table", "another_one"))
    for name in OPS_TABLES:
        assert base.tables[name].equals(extended.tables[name], null_equal=True), name
    assert base.seed_record.spawn_keys == extended.seed_record.spawn_keys


def test_spawn_order_must_start_with_known_tables(blinded: dict[str, pl.DataFrame]) -> None:
    with pytest.raises(ValueError, match="spawn_order"):
        _run(blinded, 1, ("queries", "data_entry", "deviations", "screening"))


def test_needs_only_blinded_input(blinded: dict[str, pl.DataFrame]) -> None:
    assert "ARM" not in blinded["dm"].columns
    assert set(_run(blinded, 1).tables) == set(OPS_TABLES)


def test_every_table_carries_study_and_site(blinded: dict[str, pl.DataFrame]) -> None:
    for name, frame in _run(blinded, 3).tables.items():
        assert {"STUDYID", "SITEID"} <= set(frame.columns), name
        if frame.height:
            assert set(frame["STUDYID"]) == {"MRD-201"}, name
            assert set(frame["SITEID"]) <= {"1001", "1003"}, name


def test_data_entry_grain_and_lag(blinded: dict[str, pl.DataFrame]) -> None:
    de = _run(blinded, 5).tables["data_entry"]
    assert de.height == blinded["sv"].height
    assert (de["entry_lag_days"] > 0).all()
    per_site = de.group_by("SITEID").agg(pl.col("site_median_lag_days").n_unique())
    assert (per_site["site_median_lag_days"] == 1).all()


def test_queries_open_have_age_closed_do_not(blinded: dict[str, pl.DataFrame]) -> None:
    q = _run(blinded, 11).tables["queries"]
    assert q.height > 0
    assert q.filter(pl.col("status") == "open")["age_days"].null_count() == 0
    assert (
        q.filter(pl.col("status") == "closed")["age_days"].null_count()
        == q.filter(pl.col("status") == "closed").height
    )
    assert ((q["status_draw"] < PARAMS.query_open_probability) == (q["status"] == "open")).all()
    assert q["query_id"].n_unique() == q.height


def test_deviations_belong_to_enrolled_subjects_of_their_site(
    blinded: dict[str, pl.DataFrame],
) -> None:
    dm = blinded["dm"].filter(pl.col("RFSTDTC") != "").select("USUBJID", "SITEID")
    for seed in range(20):
        dev = _run(blinded, seed).tables["deviations"]
        joined = dev.join(dm, on="USUBJID", how="left", suffix="_dm")
        assert (joined["SITEID"] == joined["SITEID_dm"]).all()
        assert set(dev["category"]) <= set(PARAMS.deviation_categories)


def test_screening_counts_are_consistent(blinded: dict[str, pl.DataFrame]) -> None:
    sc = _run(blinded, 9).tables["screening"]
    enrolled = sc.filter(pl.col("outcome") == "enrolled")
    assert enrolled.height == 4
    pilot_fail = sc.filter((pl.col("outcome") == "screen_failure") & (pl.col("source") == "pilot"))
    assert pilot_fail["SRC_USUBJID"].to_list() == ["01-701-1002"]
    synthetic = sc.filter(pl.col("source") == "synthetic")
    assert synthetic["USUBJID"].null_count() == synthetic.height
    per_site = sc.group_by("SITEID").agg(
        (pl.col("source") == "synthetic").sum().alias("n_syn"),
        pl.col("site_synthetic_failures").first(),
    )
    assert (per_site["n_syn"] == per_site["site_synthetic_failures"]).all()
    assert sc["screening_id"].n_unique() == sc.height


def test_seed_record(blinded: dict[str, pl.DataFrame]) -> None:
    record = _run(blinded, 77).seed_record
    assert record.seed == 77
    assert record.spawn_keys == {name: (i,) for i, name in enumerate(OPS_TABLES)}
    assert record.params == PARAMS
