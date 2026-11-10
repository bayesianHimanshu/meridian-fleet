"""A small base cut for TMF tests: the synthetic RBQM tables with DM end dates added."""

from pathlib import Path

import polars as pl

from meridian.faults.reader import FullCut, FullCutReader
from tests.rbqm_support import make_tables, write_tables_cut

DAYS_ON_STUDY = 200


def tmf_base(root: Path, cut_id: str = "DC-BASE") -> FullCut:
    """Every subject gets RFENDTC = RFSTDTC + 200 days (the synthetic DM has no end)."""
    blinded, ops, unblinded = make_tables()
    dm = blinded["dm"].with_columns(
        (pl.col("RFSTDTC_DATE") + pl.duration(days=DAYS_ON_STUDY))
        .dt.to_string("%Y-%m-%d")
        .fill_null("")
        .alias("RFENDTC"),
        pl.lit("").alias("RFICDTC"),
    )
    study = write_tables_cut(root / "curated", cut_id, {**blinded, "dm": dm}, ops, unblinded)
    return FullCutReader(study / cut_id).read()
