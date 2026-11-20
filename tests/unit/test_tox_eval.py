"""The preclinical evaluation harness (docs/preclinical-eval.md; 1.1.0 control base): the
base resamples control animals only, keeps group sizes and TK animals, planting records
its faults, ground truth is never rewritten."""

from pathlib import Path

import polars as pl
import pytest

from meridian.evals.preclinical import (
    CHANGE,
    RULE,
    control_base,
    evaluate_null,
    evaluate_set,
    plant,
    summarise,
)
from meridian.preclinical.analysis import read_design
from meridian.preclinical.inprocess import run_checks
from meridian.preclinical.synth import StudySpec, build
from tests.tox_support import config

RUN = run_checks(config(), "SYN-TOX-01")


def _sizes(tables: dict[str, pl.DataFrame]) -> list[tuple[object, ...]]:
    d = read_design(tables["dm"], tables["tx"], tables["ds"])
    return sorted(d.animals.group_by(["phase", "SEX", "rank"], maintain_order=True).len().rows())


def test_the_control_base_holds_only_control_animals_records() -> None:
    real = build(
        StudySpec(
            seed=51,
            recovery=True,
            effects=(),
        )
    )
    base = control_base(real, 1)
    assert _sizes(base) == _sizes(real)
    assert base["dm"].equals(real["dm"])  # ids, sexes and groups kept
    design = read_design(real["dm"], real["tx"], real["ds"])
    animals = design.animals
    controls = set(animals.filter(pl.col("rank") == 0)["USUBJID"].to_list())
    # Every animal's body weights are some control animal's of the same sex and phase.
    for uid, sex, phase in animals.select("USUBJID", "SEX", "phase").rows():
        mine = base["bw"].filter(pl.col("USUBJID") == uid)["BWSTRESN"].to_list()
        sources = animals.filter(
            pl.col("USUBJID").is_in(sorted(controls))
            & (pl.col("SEX") == sex)
            & (pl.col("phase") == phase)
        )["USUBJID"].to_list()
        assert any(
            real["bw"].filter(pl.col("USUBJID") == s)["BWSTRESN"].to_list() == mine for s in sources
        )
    assert control_base(real, 1)["lb"].equals(base["lb"])  # seeded
    assert not control_base(real, 2)["lb"].equals(base["lb"])


def test_planting_records_one_fault_per_effect_and_sex() -> None:
    planted = plant(control_base(build(StudySpec(seed=52)), 2), 2)
    kinds = sorted({f.fault_type for f in planted.faults})
    assert kinds == [
        "bw_gain_decrease",
        "consequence",
        "incidence_increase",
        "lb_decrease",
        "lb_increase",
        "neoplasm_single_animal",
        "organ_weight_increase",
        "outlier_single_animal",
        "unscheduled_death",
    ]
    assert [f.target for f in planted.faults if f.kri is None] == ["lb:CREAT:M:main"]
    assert {f.kri for f in planted.faults if f.fault_type == "lb_increase"} == {CHANGE}
    assert {f.kri for f in planted.faults if f.fault_type == "unscheduled_death"} == {RULE}
    hyper = planted.tables["mi"].filter(pl.col("MISTRESC") == "HYPERTROPHY")
    assert hyper.height == 2 * (3 + 8)
    cause = planted.tables["mi"].filter(pl.col("MIDTHREL") == "Y")
    assert cause.height == 2


def test_the_planted_death_and_neoplasm_are_found_on_a_synthetic_base(tmp_path: Path) -> None:
    base = build(StudySpec(seed=54))
    results = evaluate_set("dev", base, config(), RUN, "0" * 64, tmp_path)
    rates = summarise(results)["found_rate_by_fault"]
    assert isinstance(rates, dict)
    truth = results[0]
    assert any(c.startswith("unscheduled_death:") for c in truth.rule_findings)
    assert any(c.startswith("neoplasm:") for c in truth.rule_findings)
    nulls = evaluate_null(base, RUN, RUN)
    assert len(nulls) == 20
    assert all(r.red_cell_plain >= 0 for r in nulls)


def test_ground_truth_is_written_once_and_never_rewritten(tmp_path: Path) -> None:
    base = build(StudySpec(seed=53))
    evaluate_set("dev", base, config(), RUN, "0" * 64, tmp_path)
    first = {p.name: p.read_text() for p in tmp_path.glob("*.json")}
    assert len(first) == 10
    evaluate_set("dev", base, config(), RUN, "0" * 64, tmp_path)
    assert {p.name: p.read_text() for p in tmp_path.glob("*.json")} == first
    with pytest.raises(ValueError, match="never rewritten"):
        evaluate_set("dev", base, config(), RUN, "f" * 64, tmp_path)
