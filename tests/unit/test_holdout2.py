"""Holdout 2, the AE holdouts and opaque cut ids."""

import re
from pathlib import Path

import pytest

from meridian.faults.catalog import REGISTRY
from meridian.faults.inject import GroundTruth
from meridian.faults.reader import FullCut, FullCutReader
from meridian.faults.scenario import (
    AE_HOLDOUT_DIR,
    HOLDOUT2_FILE,
    HOLDOUT2_SEED,
    Scenario,
    eligible_sites,
    ensure_ae_holdouts,
    ensure_holdout,
    ensure_holdout2,
    ensure_rbqm_core,
    ensure_sweep,
    fault_site_pairs,
    load_scenario,
    opaque_cut_id,
)
from tests.rbqm_support import make_tables, write_tables_cut

SIZES = [40, 30, 26, 25, 24, 21, 18, 16, 13, 9, 8, 7]
OPAQUE = re.compile(r"^DC-[0-9a-f]{8}$")


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> FullCut:
    root = tmp_path_factory.mktemp("cuts")
    study = write_tables_cut(root, "DC-BASE", *make_tables(sizes=SIZES))
    return FullCutReader(study / "DC-BASE").read()


def _scenarios(directory: Path, base: FullCut) -> list[Scenario]:
    ensure_rbqm_core(directory / "rbqm_core.toml", base)
    ensure_holdout(directory / "rbqm_holdout.toml", base)
    sweep = ensure_sweep(directory / "sweep", base, load_scenario(directory / "rbqm_core.toml"))
    return [
        load_scenario(p)
        for p in [directory / "rbqm_core.toml", directory / "rbqm_holdout.toml", *sweep]
    ]


def test_opaque_cut_id_is_stable_and_names_no_fault(tmp_path: Path) -> None:
    data = b'seed = 1\nbase_cut = "DC-BASE"\n'
    assert opaque_cut_id(data) == opaque_cut_id(bytes(data))
    assert opaque_cut_id(data) != opaque_cut_id(data + b"\n")
    assert OPAQUE.match(opaque_cut_id(data))
    path = tmp_path / "x.toml"
    path.write_bytes(
        data + b'label = "ae_holdout/1"\n\n[[faults]]\ntype = "ae_under_reporting"\n'
        b'target = "1003"\n[faults.params]\ndrop_share = 0.7\n'
    )
    scenario = load_scenario(path)
    assert scenario.out_cut == opaque_cut_id(path.read_bytes())
    assert scenario.label == "ae_holdout/1"
    # Only DC- and 8 hex digits: no fault type, label or parameter can be in it.
    assert OPAQUE.match(scenario.out_cut)
    for word in [*REGISTRY, "holdout", "under", "drop", "label"]:
        assert word not in scenario.out_cut.lower()


def test_holdout2_is_reproducible_and_repeats_no_earlier_pair(
    tmp_path: Path, base: FullCut
) -> None:
    earlier = _scenarios(tmp_path, base)
    path = ensure_holdout2(tmp_path, base)
    text = path.read_text()
    assert "out_cut" not in text
    holdout2 = load_scenario(path)
    assert OPAQUE.match(holdout2.out_cut)
    assert holdout2.seed == HOLDOUT2_SEED
    assert holdout2.label == "rbqm_holdout2"
    assert [f.type for f in holdout2.faults] == [f.type for f in earlier[0].faults]
    site_faults = [f for f in holdout2.faults if f.target != "STUDY"]
    assert len({f.target for f in site_faults}) == 5
    assert {f.target for f in site_faults} <= set(eligible_sites(base))
    assert not fault_site_pairs([holdout2]) & fault_site_pairs(earlier)
    # Generated again elsewhere from the same inputs: the same bytes, the same cut id.
    other = tmp_path / "again"
    _scenarios(other, base)
    assert ensure_holdout2(other, base).read_bytes() == path.read_bytes()
    assert load_scenario(other / HOLDOUT2_FILE).out_cut == holdout2.out_cut


def test_ae_holdouts_use_three_new_sites(tmp_path: Path, base: FullCut) -> None:
    earlier = _scenarios(tmp_path, base)
    earlier.append(load_scenario(ensure_holdout2(tmp_path, base)))
    paths = ensure_ae_holdouts(tmp_path, base)
    assert [p.relative_to(tmp_path).as_posix() for p in paths] == [
        f"{AE_HOLDOUT_DIR}/{n}.toml" for n in (1, 2, 3)
    ]
    loaded = [load_scenario(p) for p in paths]
    targets = [s.faults[0].target for s in loaded]
    assert len(set(targets)) == 3
    before = {t for kind, t in fault_site_pairs(earlier) if kind == "ae_under_reporting"}
    assert not set(targets) & before
    assert set(targets) <= set(eligible_sites(base))
    assert all(s.faults[0].params == {"drop_share": 0.7} for s in loaded)
    assert len({s.out_cut for s in loaded}) == 3
    assert [s.label for s in loaded] == ["ae_holdout/1", "ae_holdout/2", "ae_holdout/3"]
    # Written once: a second call reads the files and changes nothing.
    stamps = [p.read_bytes() for p in paths]
    ensure_ae_holdouts(tmp_path, base)
    assert [p.read_bytes() for p in paths] == stamps


def test_ground_truth_without_label_keeps_its_old_bytes(tmp_path: Path) -> None:
    truth = GroundTruth(
        cut_id="DC-X",
        scenario_path="s.toml",
        scenario_sha256="0" * 64,
        seed=1,
        base_cut="DC-BASE",
        base_manifest_sha256="1" * 64,
        faults=(),
    )
    assert '"label"' not in truth.dumps()
    labelled = truth.model_copy(update={"label": "rbqm_holdout2"})
    assert '"label": "rbqm_holdout2"' in labelled.dumps()
    path = tmp_path / "t.json"
    path.write_text(labelled.dumps())
    assert GroundTruth.load(path).label == "rbqm_holdout2"
