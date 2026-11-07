"""Expected AE counts per site, five ways (phase 1 step 5 experiment).

Each variant returns, per tested site, the observed count O_i and an expected count E_i
computed without the site's own data (leave one site out), ready for the unchanged
quasi-Poisson test in meridian.stats.robust. Treated subjects only (RFSTDTC not blank).
The treatment arm is blinded and is not used, directly or indirectly.

    a subject_days        E_i = other sites' AEs per subject-day x site subject-days
    b per_subject         E_i = other sites' AEs per treated subject x site treated subjects
    c early_window        O_i, E_i from AEs with onset in treatment days 1..W only, with
                          each subject's exposure capped at W days
    d covariate_adjusted  E_i = sum over the site's subjects of a Poisson regression fitted
                          on the other sites: age group, sex, log(subject-days) offset
    e lab_concordance     E_i = other sites' AEs per abnormal lab result x site abnormal
                          lab results (LBNRIND HIGH, LOW or ABNORMAL)
"""

from typing import Literal

import numpy as np
import polars as pl
from numpy.typing import NDArray

from meridian.data.derived import subject_days, treated_per_site, treated_subjects
from meridian.stats.poisson_glm import fit_poisson
from meridian.stats.robust import ExpectedCount, leave_one_out
from meridian.stats.site_metrics import counts_by_site, exposure

AeVariant = Literal["a", "b", "c", "d", "e"]
AE_VARIANTS: dict[str, str] = {
    "a": "subject_days",
    "b": "per_subject",
    "c": "early_window",
    "d": "covariate_adjusted",
    "e": "lab_concordance",
}
ABNORMAL_LAB = ("HIGH", "LOW", "ABNORMAL")
AGE_GROUPS = ("<70", "70-79", ">=80")


def _sum_by_site(frame: pl.DataFrame, column: str) -> dict[str, float]:
    out = frame.group_by("SITEID").agg(pl.col(column).sum()).sort("SITEID")
    return {str(s): float(v) for s, v in out.iter_rows()}


def subject_days_variant(
    dm: pl.DataFrame, sv: pl.DataFrame, ae: pl.DataFrame, tested: list[str]
) -> list[ExpectedCount]:
    exp = exposure(dm, sv)
    counts = counts_by_site(ae, exp.per_subject.select("USUBJID", "SITEID"))
    return leave_one_out(counts, exp.days(), tested)


def per_subject_variant(
    dm: pl.DataFrame, ae: pl.DataFrame, tested: list[str]
) -> list[ExpectedCount]:
    treated = treated_subjects(dm)
    counts = counts_by_site(ae, treated)
    units = {str(s): float(n) for s, n in treated_per_site(dm).iter_rows()}
    return leave_one_out(counts, units, tested)


def onset_day(dm: pl.DataFrame, ae: pl.DataFrame) -> pl.DataFrame:
    """AE rows of treated subjects with SITEID and onset day (AESTDTC - RFSTDTC + 1)."""
    return (
        ae.select("USUBJID", "AESTDTC_DATE")
        .join(treated_subjects(dm), on="USUBJID", how="inner")
        .with_columns(
            ((pl.col("AESTDTC_DATE") - pl.col("RFSTDTC_DATE")).dt.total_days() + 1).alias(
                "onset_day"
            )
        )
    )


def early_window_variant(
    dm: pl.DataFrame, sv: pl.DataFrame, ae: pl.DataFrame, tested: list[str], window_days: int
) -> list[ExpectedCount]:
    included = subject_days(dm, sv).filter(pl.col("subject_days").is_not_null())
    capped = included.with_columns(
        pl.min_horizontal(pl.col("subject_days"), pl.lit(window_days)).alias("capped")
    )
    early = onset_day(dm, ae).filter(
        pl.col("USUBJID").is_in(included["USUBJID"].to_list())
        & pl.col("onset_day").is_between(1, window_days)
    )
    counts = counts_by_site(early, included.select("USUBJID", "SITEID"))
    return leave_one_out(counts, _sum_by_site(capped, "capped"), tested)


def design(subjects: pl.DataFrame) -> NDArray[np.float64]:
    """Intercept, age 70-79, age >= 80, male. Reference: age < 70, female."""
    age = subjects["AGE"].to_numpy().astype(np.float64)
    male = (subjects["SEX"] == "M").to_numpy().astype(np.float64)
    middle = ((age >= 70) & (age < 80)).astype(np.float64)
    old = (age >= 80).astype(np.float64)
    return np.column_stack([np.ones(len(age)), middle, old, male])


def covariate_adjusted_variant(
    dm: pl.DataFrame, sv: pl.DataFrame, ae: pl.DataFrame, tested: list[str]
) -> list[ExpectedCount]:
    """One Poisson regression per tested site, fitted on every other site's subjects.

    Subjects need subject-days (the offset). A dummy column that is constant in the
    training subjects is dropped from that fit (its coefficient is 0 for prediction).
    """
    days = subject_days(dm, sv).filter(pl.col("subject_days").is_not_null())
    per_subject = (
        days.join(dm.select("USUBJID", "AGE", "SEX"), on="USUBJID", how="left")
        .join(
            ae.select("USUBJID").group_by("USUBJID", maintain_order=True).len("y"),
            on="USUBJID",
            how="left",
        )
        .with_columns(pl.col("y").fill_null(0))
        .sort("USUBJID")
    )
    x_all = design(per_subject)
    y_all = per_subject["y"].to_numpy().astype(np.float64)
    off_all = np.log(per_subject["subject_days"].to_numpy().astype(np.float64))
    sites = per_subject["SITEID"].to_numpy()
    out: list[ExpectedCount] = []
    for site in tested:
        own = sites == site
        if not own.any() or own.all():
            continue
        train = ~own
        keep = [0] + [j for j in range(1, x_all.shape[1]) if np.ptp(x_all[train, j]) > 0]
        fit = fit_poisson(x_all[train][:, keep], y_all[train], off_all[train])
        expected = float(fit.predict(x_all[own][:, keep], off_all[own]).sum())
        if expected <= 0:
            continue
        site_days = float(np.exp(off_all[own]).sum())
        out.append(
            ExpectedCount(
                site=site,
                observed=int(y_all[own].sum()),
                expected=expected,
                units=expected,
                rate_others=expected / site_days,
            )
        )
    return out


def lab_concordance_variant(
    dm: pl.DataFrame, ae: pl.DataFrame, lb: pl.DataFrame, tested: list[str]
) -> list[ExpectedCount]:
    treated = treated_subjects(dm)
    counts = counts_by_site(ae, treated)
    abnormal = lb.filter(pl.col("LBNRIND").is_in(ABNORMAL_LAB))
    units = {s: float(n) for s, n in counts_by_site(abnormal, treated).items()}
    return leave_one_out(counts, units, tested)


def ae_expectation(
    variant: AeVariant,
    *,
    dm: pl.DataFrame,
    sv: pl.DataFrame,
    ae: pl.DataFrame,
    lb: pl.DataFrame,
    tested: list[str],
    early_window_days: int,
) -> list[ExpectedCount]:
    match variant:
        case "a":
            return subject_days_variant(dm, sv, ae, tested)
        case "b":
            return per_subject_variant(dm, ae, tested)
        case "c":
            return early_window_variant(dm, sv, ae, tested, early_window_days)
        case "d":
            return covariate_adjusted_variant(dm, sv, ae, tested)
        case "e":
            return lab_concordance_variant(dm, ae, lb, tested)
