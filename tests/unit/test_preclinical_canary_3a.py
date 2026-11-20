"""The canary in the preclinical zone's step 2 inputs (phase 5 step 3a): the NOAEL
extract and an inventory cut, each in a column its agent reads but never puts in a
finding, so a finding can never carry it and any copy found elsewhere is a leak."""

from pathlib import Path

import polars as pl

from meridian.preclinical.inputs import write_inventory
from meridian.preclinical.inventory import INVENTORY_FILE
from meridian.sandbox.substrate_proofs import copy_inventory, plant_noael_extract
from tests.unit.test_dose_translation import _decision

ROOT = Path(__file__).resolve().parents[2]
CANARY = "MERIDIAN-CANARY-0123456789abcdef"


def test_the_canary_goes_into_the_noael_extract_and_the_inventory(tmp_path: Path) -> None:
    from meridian.preclinical.inputs import write_noael_extract

    events = [_decision(10, "combined", 2.0, 20.0)]
    written = write_noael_extract(events, events[-1], "TOX-FINAL", tmp_path / "noael")
    root, rel = plant_noael_extract(canary=CANARY, dest=tmp_path, written=written, trigger_seq=10)
    assert root == tmp_path / "noael"
    assert rel == "noael/10/noael/noael_decisions.parquet"
    assert pl.read_parquet(tmp_path / rel)["recorded_at"][0] == CANARY
    cut, _, _ = write_inventory(ROOT / INVENTORY_FILE, tmp_path / "src", "MRD-101")
    inv, rel = copy_inventory(
        canary=CANARY,
        dest=tmp_path / "copy",
        inventory_root=tmp_path / "src",
        asset_id="MRD-101",
        cut_id=cut,
    )
    assert inv == tmp_path / "copy" / "inventory"
    frame = pl.read_parquet(tmp_path / "copy" / rel)
    assert frame["route"][0] == CANARY
    assert CANARY.encode() in (tmp_path / "copy" / rel).read_bytes()
