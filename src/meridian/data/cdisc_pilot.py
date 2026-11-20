"""Pure transform from CDISC pilot SDTM frames to the curated MRD-201 tables.

No file I/O happens here. Input is one polars DataFrame per domain; output is the
blinded and unblinded tables, ready for the cut writer.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import polars as pl

SDTM_DOMAINS: tuple[str, ...] = ("dm", "ae", "vs", "lb", "ds", "sv", "ex")
STUDY_PREFIX = "201"

RANDOMIZATION_COLUMNS: tuple[str, ...] = ("ARMCD", "ARM", "ACTARMCD", "ACTARM")
EX_DATES_COLUMNS: tuple[str, ...] = (
    "STUDYID",
    "USUBJID",
    "EXSEQ",
    "VISIT",
    "VISITNUM",
    "EXSTDTC",
    "EXENDTC",
)

# Stable sort key per curated table. Ties keep source order (maintain_order=True).
SORT_KEYS: dict[str, tuple[str, ...]] = {
    "dm": ("USUBJID",),
    "ae": ("USUBJID", "AESEQ"),
    "vs": ("USUBJID", "VSSEQ"),
    "lb": ("USUBJID", "LBSEQ"),
    "ds": ("USUBJID", "DSSEQ"),
    "sv": ("USUBJID", "VISITNUM", "SVSTDTC"),
    "ex": ("USUBJID", "EXSEQ"),
    "ex_dates": ("USUBJID", "EXSEQ"),
    "randomization": ("USUBJID",),
}

_FULL_DATE = r"^(\d{4}-\d{2}-\d{2})(?:T.*)?$"


class TransformError(Exception):
    """Raised when source frames do not have the shape the transform needs."""


@dataclass(frozen=True, slots=True)
class CuratedStudy:
    blinded: Mapping[str, pl.DataFrame]
    unblinded: Mapping[str, pl.DataFrame]
    subject_map: pl.DataFrame


def _require(frame: pl.DataFrame, domain: str, columns: tuple[str, ...]) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise TransformError(f"{domain}: missing required column(s) {missing}")


def build_subject_map(dm: pl.DataFrame, site_map: Mapping[str, str]) -> pl.DataFrame:
    """SRC_USUBJID to new USUBJID "201-<site>-<nnn>", nnn numbered within site by SRC_USUBJID."""
    _require(dm, "dm", ("USUBJID", "SITEID"))
    if dm["USUBJID"].n_unique() != dm.height:
        raise TransformError("dm: USUBJID is not unique")
    unknown = sorted(set(dm["SITEID"].to_list()) - set(site_map))
    if unknown:
        raise TransformError(f"dm: SITEID value(s) {unknown} are not in the site map")
    return (
        dm.select(
            pl.col("USUBJID").alias("SRC_USUBJID"),
            pl.col("SITEID").alias("SRC_SITEID"),
            pl.col("SITEID").replace_strict(dict(site_map), return_dtype=pl.String).alias("SITEID"),
        )
        .sort("SRC_USUBJID")
        .with_columns(pl.int_range(1, pl.len() + 1).over("SITEID").alias("_n"))
        .with_columns(
            pl.col("_n").cast(pl.String).str.zfill(3).alias("SUBJID"),
        )
        .with_columns(
            pl.concat_str(
                [pl.lit(STUDY_PREFIX), pl.col("SITEID"), pl.col("SUBJID")], separator="-"
            ).alias("USUBJID")
        )
        .select("SRC_USUBJID", "USUBJID", "SUBJID", "SITEID", "SRC_SITEID")
        .sort("USUBJID")
    )


def _relabel(
    frame: pl.DataFrame, domain: str, subjects: pl.DataFrame, study_id: str
) -> pl.DataFrame:
    """Swap USUBJID for the new id (keeping SRC_USUBJID next to it) and set STUDYID."""
    _require(frame, domain, ("STUDYID", "USUBJID"))
    unknown = sorted(set(frame["USUBJID"].to_list()) - set(subjects["SRC_USUBJID"].to_list()))
    if unknown:
        raise TransformError(f"{domain}: {len(unknown)} USUBJID(s) not in dm, e.g. {unknown[:3]}")
    mapping = dict(zip(subjects["SRC_USUBJID"], subjects["USUBJID"], strict=True))
    order: list[str] = []
    for column in frame.columns:
        order.append(column)
        if column == "USUBJID":
            order.append("SRC_USUBJID")
    return frame.with_columns(
        pl.col("USUBJID").alias("SRC_USUBJID"),
        pl.col("USUBJID").replace_strict(mapping, return_dtype=pl.String),
        pl.lit(study_id, dtype=pl.String).alias("STUDYID"),
    ).select(order)


def add_parsed_dates(frame: pl.DataFrame) -> pl.DataFrame:
    """For each string --DTC column add <name>_DATE right after it.

    Only a complete YYYY-MM-DD (optionally followed by a time) parses; partial or
    blank values give null. The original string column is kept unchanged.
    """
    order: list[str] = []
    parsed: list[pl.Expr] = []
    for column, dtype in frame.schema.items():
        order.append(column)
        if column.endswith("DTC") and dtype == pl.String:
            name = f"{column}_DATE"
            order.append(name)
            parsed.append(
                pl.col(column)
                .str.extract(_FULL_DATE, 1)
                .str.to_date("%Y-%m-%d", strict=False)
                .alias(name)
            )
    return frame.with_columns(parsed).select(order) if parsed else frame


def _relabel_dm_sites(dm: pl.DataFrame, subjects: pl.DataFrame) -> pl.DataFrame:
    """In DM, SUBJID becomes the within-site number and SITEID the Meridian site id.

    The pilot SITEID is kept as SRC_SITEID, right after SITEID, for lineage.
    """
    _require(dm, "dm", ("SUBJID", "SITEID"))
    subjid = dict(zip(subjects["USUBJID"], subjects["SUBJID"], strict=True))
    site = dict(zip(subjects["USUBJID"], subjects["SITEID"], strict=True))
    order: list[str] = []
    for column in dm.columns:
        order.append(column)
        if column == "SITEID":
            order.append("SRC_SITEID")
    return dm.with_columns(
        pl.col("SITEID").alias("SRC_SITEID"),
        pl.col("USUBJID").replace_strict(subjid, return_dtype=pl.String).alias("SUBJID"),
        pl.col("USUBJID").replace_strict(site, return_dtype=pl.String).alias("SITEID"),
    ).select(order)


def _sorted(name: str, frame: pl.DataFrame) -> pl.DataFrame:
    return frame.sort(list(SORT_KEYS[name]), maintain_order=True, nulls_last=True)


def curate(
    frames: Mapping[str, pl.DataFrame], site_map: Mapping[str, str], study_id: str
) -> CuratedStudy:
    missing = [d for d in SDTM_DOMAINS if d not in frames]
    if missing:
        raise TransformError(f"missing domain frame(s): {missing}")
    subjects = build_subject_map(frames["dm"], site_map)
    relabelled = {
        d: add_parsed_dates(_relabel(frames[d], d, subjects, study_id)) for d in SDTM_DOMAINS
    }

    dm = _relabel_dm_sites(relabelled["dm"], subjects)
    _require(dm, "dm", RANDOMIZATION_COLUMNS)
    randomization = dm.select("STUDYID", "USUBJID", "SRC_USUBJID", *RANDOMIZATION_COLUMNS)
    dm_blinded = dm.drop(*RANDOMIZATION_COLUMNS)

    ex = relabelled["ex"]
    _require(ex, "ex", EX_DATES_COLUMNS)
    ex_keep = [
        c
        for c in ex.columns
        if c in EX_DATES_COLUMNS or c == "SRC_USUBJID" or c in {"EXSTDTC_DATE", "EXENDTC_DATE"}
    ]
    ex_dates = ex.select(ex_keep)

    blinded = {
        "dm": dm_blinded,
        "ae": relabelled["ae"],
        "vs": relabelled["vs"],
        "lb": relabelled["lb"],
        "ds": relabelled["ds"],
        "sv": relabelled["sv"],
        "ex_dates": ex_dates,
    }
    unblinded = {"randomization": randomization, "ex": ex}
    return CuratedStudy(
        blinded={k: _sorted(k, v) for k, v in blinded.items()},
        unblinded={k: _sorted(k, v) for k, v in unblinded.items()},
        subject_map=subjects,
    )
