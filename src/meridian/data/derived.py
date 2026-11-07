"""Derived study quantities shared by agents, fault injection and reports.

Everything here works on blinded tables only and uses the step 2 rule: a subject is
treated when RFSTDTC is not blank.
"""

import polars as pl

DISPOSITION_EVENT = "DISPOSITION EVENT"
COMPLETED = "COMPLETED"


def treated_expr() -> pl.Expr:
    return pl.col("RFSTDTC").fill_null("").str.strip_chars() != ""


def treated_subjects(dm: pl.DataFrame) -> pl.DataFrame:
    """USUBJID, SITEID and RFSTDTC_DATE of treated subjects, sorted by USUBJID."""
    return dm.filter(treated_expr()).select("USUBJID", "SITEID", "RFSTDTC_DATE").sort("USUBJID")


def treated_per_site(dm: pl.DataFrame) -> pl.DataFrame:
    """SITEID, treated (count), sorted by SITEID."""
    return treated_subjects(dm).group_by("SITEID").agg(pl.len().alias("treated")).sort("SITEID")


def subject_days(dm: pl.DataFrame, sv: pl.DataFrame) -> pl.DataFrame:
    """Per treated subject: last SV visit date minus RFSTDTC date plus one.

    Columns: USUBJID, SITEID, subject_days (null when either date is missing).
    """
    last_visit = sv.group_by("USUBJID", maintain_order=True).agg(
        pl.col("SVSTDTC_DATE").max().alias("_last")
    )
    return (
        treated_subjects(dm)
        .join(last_visit, on="USUBJID", how="left")
        .with_columns(
            ((pl.col("_last") - pl.col("RFSTDTC_DATE")).dt.total_days() + 1).alias("subject_days")
        )
        .select("USUBJID", "SITEID", "subject_days")
        .sort("USUBJID")
    )


def disposition(dm: pl.DataFrame, ds: pl.DataFrame) -> pl.DataFrame:
    """Per treated subject: USUBJID, SITEID, DSDECOD of the DISPOSITION EVENT (or null)."""
    events = (
        ds.filter(pl.col("DSCAT") == DISPOSITION_EVENT)
        .sort("USUBJID", "DSSEQ")
        .group_by("USUBJID", maintain_order=True)
        .agg(pl.col("DSDECOD").last())
    )
    return (
        treated_subjects(dm)
        .select("USUBJID", "SITEID")
        .join(events, on="USUBJID", how="left")
        .sort("USUBJID")
    )


def premature_discontinuation(dm: pl.DataFrame, ds: pl.DataFrame) -> tuple[int, int]:
    """(discontinued, treated): treated subjects whose disposition is not COMPLETED.

    A treated subject with no disposition event is not counted as discontinued.
    """
    disp = disposition(dm, ds)
    discontinued = disp.filter(
        pl.col("DSDECOD").is_not_null() & (pl.col("DSDECOD") != COMPLETED)
    ).height
    return discontinued, disp.height
