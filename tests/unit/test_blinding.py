from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from meridian.data.blinding import BlindingViolationError, assert_blinded, forbidden_columns_in
from meridian.data.cut_writer import write_cut

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def test_containment_is_case_insensitive() -> None:
    cols = ["USUBJID", "ARMCD", "src_arm", "TRT01P_FLAG", "EXDOSE", "VISIT", "AETERM"]
    assert forbidden_columns_in(cols) == ["ARMCD", "src_arm", "TRT01P_FLAG", "EXDOSE"]


def test_assert_blinded_passes_clean_frame() -> None:
    assert_blinded("dm", pl.DataFrame({"USUBJID": ["a"], "SEX": ["F"]}))


def _write(tmp_path: Path, blinded: dict[str, pl.DataFrame], ops: dict[str, pl.DataFrame]) -> None:
    write_cut(
        curated_root=tmp_path,
        study_id="MRD-201",
        cut_id="DC-X",
        blinded=blinded,
        ops=ops,
        unblinded={"randomization": pl.DataFrame({"USUBJID": ["a"], "ARM": ["Placebo"]})},
        created_at=NOW,
        seed=1,
        seed_record={},
        sources=[],
    )


@pytest.mark.parametrize("area", ["blinded", "ops"])
def test_writer_raises_before_writing_anything(tmp_path: Path, area: str) -> None:
    leaky = {"dm": pl.DataFrame({"USUBJID": ["a"], "ACTARM": ["Placebo"]})}
    clean = {"sv": pl.DataFrame({"USUBJID": ["a"]})}
    blinded, ops = (leaky, clean) if area == "blinded" else (clean, leaky)
    with pytest.raises(BlindingViolationError, match="ACTARM"):
        _write(tmp_path, blinded, ops)
    assert list(tmp_path.iterdir()) == []


def test_unblinded_area_may_hold_forbidden_columns(tmp_path: Path) -> None:
    _write(tmp_path, {"dm": pl.DataFrame({"USUBJID": ["a"]})}, {})
    assert (tmp_path / "MRD-201" / "DC-X" / "unblinded" / "randomization.parquet").exists()
