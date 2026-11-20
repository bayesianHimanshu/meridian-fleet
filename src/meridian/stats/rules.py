"""The measured values behind the rule checks, shared by the agents and the eval.

The agents raise a rule finding exactly where `met` is true; the eval computes the same
values from the same cut to get condition truth.
"""

from dataclasses import dataclass
from enum import StrEnum

import polars as pl

from meridian.data.derived import premature_discontinuation, treated_subjects

STUDY_UNIT = "STUDY"


@dataclass(frozen=True, slots=True)
class RuleValue:
    """One unit (a site, or the study) of a rule check."""

    check: str
    site: str
    value: float | None
    n: int
    threshold: float
    met: bool
    count: int | None = None  # numerator of a rate (discontinued subjects for the QTL)


def _site_medians(rows: pl.DataFrame, column: str) -> list[tuple[str, float | None, int]]:
    out = (
        rows.group_by("SITEID")
        .agg(pl.col(column).median().alias("median"), pl.len().alias("n"))
        .sort("SITEID")
    )
    return [(str(s), None if m is None else float(m), int(n)) for s, m, n in out.iter_rows()]


def entry_lag(dm: pl.DataFrame, data_entry: pl.DataFrame, lag_days: float) -> list[RuleValue]:
    """Median entry lag per site over treated subjects; met when above lag_days."""
    treated = treated_subjects(dm)["USUBJID"].to_list()
    rows = data_entry.filter(pl.col("USUBJID").is_in(treated))
    return [
        RuleValue(
            "data_entry_lag", site, median, n, lag_days, median is not None and median > lag_days
        )
        for site, median, n in _site_medians(rows, "entry_lag_days")
    ]


def query_aging(
    dm: pl.DataFrame, queries: pl.DataFrame, query_age_days: float, min_open_queries: int
) -> list[RuleValue]:
    """Median age of open queries per site over treated subjects.

    Met when the site has at least min_open_queries open queries and the median is above
    query_age_days.
    """
    treated = treated_subjects(dm)["USUBJID"].to_list()
    rows = queries.filter(pl.col("USUBJID").is_in(treated) & (pl.col("status") == "open"))
    return [
        RuleValue(
            "query_aging",
            site,
            median,
            n,
            query_age_days,
            n >= min_open_queries and median is not None and median > query_age_days,
        )
        for site, median, n in _site_medians(rows, "age_days")
    ]


class QtlStatus(StrEnum):
    WITHIN = "within"
    APPROACHING = "approaching"
    BREACHED = "breached"


def qtl_status(rate: float, limit: float, approach_band: float) -> QtlStatus:
    if rate >= limit:
        return QtlStatus.BREACHED
    if rate >= limit - approach_band:
        return QtlStatus.APPROACHING
    return QtlStatus.WITHIN


def discontinuation(
    dm: pl.DataFrame, ds: pl.DataFrame, limit: float, approach_band: float
) -> RuleValue:
    """Study-level premature discontinuation; met when approaching or breached.

    The QTL detector raises a finding in both states, so both count as the condition.
    The threshold recorded is where approaching starts.
    """
    discontinued, treated = premature_discontinuation(dm, ds)
    rate = discontinued / treated if treated else None
    met = rate is not None and qtl_status(rate, limit, approach_band) is not QtlStatus.WITHIN
    return RuleValue(
        "premature_discontinuation",
        STUDY_UNIT,
        rate,
        treated,
        limit - approach_band,
        met,
        count=discontinued,
    )
