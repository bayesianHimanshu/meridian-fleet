"""The IND readiness pre-registration (phase 5 step 2), committed before the agent: the
inventory regenerates from its seed byte for byte, every requirement cites its guidance,
and every stress case is well formed (docs/ind-readiness-eval.md)."""

import csv
import io
import json
import tomllib
from pathlib import Path

import pytest

from meridian.preclinical.inventory import (
    COLUMNS,
    GLP_VALUES,
    INVENTORY_FILE,
    REPORT_STATUSES,
    generate_inventory,
    inventory_csv,
)

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "config" / "ind_requirements.toml"
STRESS = ROOT / "tests" / "fixtures" / "ind_stress"


def test_the_committed_inventory_is_the_seeded_one() -> None:
    assert (ROOT / INVENTORY_FILE).read_text(encoding="utf-8") == inventory_csv(
        generate_inventory()
    )


def test_the_inventory_holds_the_real_study_and_valid_values() -> None:
    rows = list(csv.DictReader(io.StringIO((ROOT / INVENTORY_FILE).read_text())))
    assert tuple(rows[0]) == COLUMNS
    tox01 = rows[0]
    assert (tox01["study_id"], tox01["species"], tox01["duration_weeks"]) == (
        "MRD-101-TOX-01",
        "RAT",
        "13",
    )
    assert tox01["start_date"] == "2016-01-15"  # TS STSTDTC of the PointCross study
    for r in rows:
        assert r["report_status"] in REPORT_STATUSES
        assert r["glp"] in GLP_VALUES
    assert len({r["study_id"] for r in rows}) == len(rows)


def _requirements() -> dict[str, object]:
    with REQUIREMENTS.open("rb") as handle:
        return tomllib.load(handle)


def test_every_requirement_cites_a_source_section_and_quote() -> None:
    req = _requirements()
    items = req["requirements"]
    assert isinstance(items, list)
    assert len(items) == 11
    classes = req["classes"]
    assert isinstance(classes, dict)
    for r in items:
        assert r["source"], r["id"]
        assert r["section"], r["id"]
        assert len(r["quote"]) > 40, r["id"]
        assert r["species_class"] in (*classes, "any"), r["id"]
        assert r["duration"] in ("table1", "none")
        assert r["applies"] in ("always", "multiple_dose")
    assert len({r["id"] for r in items}) == len(items)


def _cases() -> list[Path]:
    return sorted(p for p in STRESS.glob("*.json") if not p.name.startswith("_"))


def test_there_are_twelve_stress_cases() -> None:
    assert len(_cases()) == 12


@pytest.mark.parametrize("path", _cases(), ids=lambda p: p.stem)
def test_a_stress_case_is_well_formed(path: Path) -> None:
    case = json.loads(path.read_text())
    assert case["base"] in ("real", "complete", "empty")
    ideal = case["ideal"]["1.0.0"]
    assert ideal["findings"] == sorted(ideal["findings"])
    assert 0.0 <= ideal["percent_met"] <= 100.0
    ids = {r.study_id for r in generate_inventory()} | {"MRD-101-DM-01"}
    for change in case["changes"]:
        (op,) = set(change) - {"fields"}
        assert op in ("drop", "set", "add")
        if op in ("drop", "set"):
            assert change[op] in ids, change
        else:
            assert set(change["add"]) == set(COLUMNS)
    for key in case.get("trial", {}):
        assert key in ("planned_clinical_duration_days", "multiple_dose", "planned_submission_date")
