import tomllib
from pathlib import Path

import pytest

from meridian.data.derived import premature_discontinuation
from meridian.faults.reader import FullCut, FullCutReader
from meridian.faults.scenario import (
    RBQM_HOLDOUT_SEED,
    SWEEP_BASE_SEED,
    Scenario,
    ensure_sweep,
    generate_holdout,
    generate_rbqm_core,
    sweep_items,
)
from tests.rbqm_support import make_tables, write_tables_cut

# Treated subjects per site, shaped like the pilot: ranks 10 and later are under 10.
SIZES = [40, 30, 26, 25, 24, 21, 18, 16, 13, 9, 8, 7]


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> FullCut:
    root = tmp_path_factory.mktemp("cuts")
    study = write_tables_cut(root, "DC-BASE", *make_tables(sizes=SIZES))
    return FullCutReader(study / "DC-BASE").read()


@pytest.fixture(scope="module")
def core(base: FullCut) -> Scenario:
    return Scenario.model_validate(tomllib.loads(generate_rbqm_core(base)))


def _parse(text: str) -> Scenario:
    return Scenario.model_validate(tomllib.loads(text))


def test_holdout_uses_ranks_6_to_10_and_falls_back_without_reusing_core_sites(
    base: FullCut, core: Scenario
) -> None:
    holdout = _parse(generate_holdout(base, core))
    assert holdout.seed == RBQM_HOLDOUT_SEED
    assert holdout.out_cut == "DC-RBQM-HOLDOUT"
    targets = [(f.type, f.target) for f in holdout.faults]
    # Ranks: 1001 40, 1002 30, 1003 26, 1004 25, 1005 24, 1006 21, 1007 18, 1008 16,
    # 1009 13, 1010 9 (skipped, under 10). Four eligible; the fifth fault falls back:
    # rank 5 (1005) was deviation_cluster's core site, so rank 4 (1004).
    assert targets[:5] == [
        ("fabricated_vitals", "1006"),
        ("ae_under_reporting", "1007"),
        ("entry_lag_spike", "1008"),
        ("query_backlog", "1009"),
        ("deviation_cluster", "1004"),
    ]
    core_sites = {f.type: f.target for f in core.faults}
    for fault_type, site in targets:
        assert core_sites[fault_type] != site or site == "STUDY"
    assert [f.params for f in holdout.faults[:5]] == [f.params for f in core.faults[:5]]
    discontinued, n = premature_discontinuation(base.blinded["dm"], base.blinded["ds"])
    assert holdout.faults[5].params == {"target_rate": round(discontinued / n + 0.08, 3)}
    text = generate_holdout(base, core)
    assert "falls back to rank 4" in text
    assert "Skipped for size: 1010 (rank 10, 9)" in text


def test_sweep_items_are_one_fault_per_cut_at_the_core_site(
    tmp_path: Path, base: FullCut, core: Scenario
) -> None:
    items = sweep_items()
    assert len(items) == 18
    paths = ensure_sweep(tmp_path / "sweep", base, core)
    core_sites = {f.type: f.target for f in core.faults}
    discontinued, n = premature_discontinuation(base.blinded["dm"], base.blinded["ds"])
    for item, path in zip(items, paths, strict=True):
        scenario = _parse(path.read_text())
        assert scenario.seed == SWEEP_BASE_SEED + item.index
        assert scenario.out_cut == item.out_cut
        (fault,) = scenario.faults
        assert fault.type == item.fault_type
        assert fault.target == core_sites[item.fault_type]
        expected = (
            round(discontinued / n + item.level, 3)
            if item.fault_type == "discontinuation_drift"
            else item.level
        )
        assert fault.params[item.parameter] == expected
    assert items[0].out_cut == "DC-SWEEP-FABRICATED-VITALS-0P75"
    assert items[-1].file_name == "discontinuation_drift_plus0p1.toml"


def test_sweep_files_are_never_rewritten(tmp_path: Path, base: FullCut, core: Scenario) -> None:
    (first,) = ensure_sweep(tmp_path, base, core)[:1]
    first.write_text(first.read_text() + "# hand note\n")
    ensure_sweep(tmp_path, base, core)
    assert first.read_text().endswith("# hand note\n")
