"""The translational bridge pre-registration (phase 5 step 3a), committed before the
agent: every mapping entry cites its sources, and every stress case is well formed
(docs/translational-notes.md)."""

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAPPING = ROOT / "config" / "translational_monitoring.toml"
STRESS = ROOT / "tests" / "fixtures" / "bridge_stress"
REQUIRED = {"liver", "heart", "kidney", "haematology", "body_weight"}


def test_the_mapping_covers_the_required_organs_and_cites_each() -> None:
    with MAPPING.open("rb") as handle:
        m = tomllib.load(handle)
    organs = {o["id"]: o for o in m["organs"]}
    assert set(organs) >= REQUIRED
    for o in m["organs"]:
        for key in ("measure", "frequency", "review", "stopping"):
            assert o[key], (o["id"], key)
        assert o["matches"], o["id"]
        assert o["sources"], o["id"]
        for s in o["sources"]:
            assert s["source"], o["id"]
            assert s["section"], o["id"]
            assert len(s["quote"]) >= 10, o["id"]
    assert m["basis"]["quote"].startswith("For example, additional parameters")


def _cases() -> list[Path]:
    return sorted(STRESS.glob("*.json"))


def test_there_are_ten_cases() -> None:
    assert len(_cases()) == 10


@pytest.mark.parametrize("path", _cases(), ids=lambda p: p.stem)
def test_a_stress_case_is_well_formed(path: Path) -> None:
    case = json.loads(path.read_text())
    ids = {f["finding_id"] for f in case["findings"]}
    for d in case["decisions"]:
        assert set(d["finding_ids"]) <= ids
    ideal = case["ideal"]["1.0.0"]
    mapped = {i for v in ideal["organs"].values() for i in v}
    assert mapped.isdisjoint(ideal["unmapped"])
    assert set(ideal["organs"]) <= REQUIRED
