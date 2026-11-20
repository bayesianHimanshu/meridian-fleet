"""Per-site quantities on blinded tables, shared by agents and eval reports."""

import math
from dataclasses import dataclass

import numpy as np
import polars as pl

from meridian.data.derived import subject_days, treated_per_site, treated_subjects
from meridian.stats.robust import robust_z


@dataclass(frozen=True, slots=True)
class Exposure:
    """Per site: treated subjects, subjects with subject-days, excluded, total subject-days."""

    frame: pl.DataFrame  # SITEID, treated, included, excluded, subject_days (sorted by SITEID)
    per_subject: pl.DataFrame  # USUBJID, SITEID, subject_days (non-null only)

    def row(self, site: str) -> dict[str, int]:
        found = self.frame.filter(pl.col("SITEID") == site)
        return {k: int(found[k][0]) for k in ("treated", "included", "excluded", "subject_days")}

    def days(self) -> dict[str, float]:
        return {s: float(d) for s, d in self.frame.select("SITEID", "subject_days").iter_rows()}


def exposure(dm: pl.DataFrame, sv: pl.DataFrame) -> Exposure:
    days = subject_days(dm, sv)
    included = days.filter(pl.col("subject_days").is_not_null())
    frame = (
        treated_per_site(dm)
        .join(
            days.group_by("SITEID", maintain_order=True).agg(
                pl.col("subject_days").is_not_null().sum().alias("included"),
                pl.col("subject_days").is_null().sum().alias("excluded"),
                pl.col("subject_days").sum().alias("subject_days"),
            ),
            on="SITEID",
            how="left",
        )
        .with_columns(pl.col("included", "excluded", "subject_days").fill_null(0))
        .sort("SITEID")
    )
    return Exposure(frame=frame, per_subject=included)


def counts_by_site(rows: pl.DataFrame, subjects: pl.DataFrame) -> dict[str, int]:
    """Rows per site, for the subjects in `subjects` (USUBJID, SITEID)."""
    joined = rows.select("USUBJID").join(subjects.select("USUBJID", "SITEID"), on="USUBJID")
    return dict(joined.group_by("SITEID").len().sort("SITEID").iter_rows())


def sites_with_min_treated(dm: pl.DataFrame, min_subjects: int) -> tuple[list[str], list[str]]:
    """(tested, skipped) site ids by treated count, both sorted."""
    sizes = treated_per_site(dm)
    tested = sizes.filter(pl.col("treated") >= min_subjects)["SITEID"].to_list()
    skipped = sizes.filter(pl.col("treated") < min_subjects)["SITEID"].to_list()
    return sorted(tested), sorted(skipped)


@dataclass(frozen=True, slots=True)
class VitalSiteStat:
    site: str
    n: int
    sd: float
    log_sd: float
    sd_ratio: float  # site SD / median site SD
    sd_z: float | None  # robust z of log SD across tested sites; None when MAD is 0
    zero_share: float | None  # share of integer readings ending in 0; None if too few
    zero_z: float | None


@dataclass(frozen=True, slots=True)
class VitalParameterStats:
    parameter: str
    sites: dict[str, VitalSiteStat]
    sd_mad_zero: bool
    zero_mad_zero: bool


def vital_stats(
    dm: pl.DataFrame,
    vs: pl.DataFrame,
    tested: list[str],
    parameters: tuple[str, ...],
    min_readings: int,
) -> list[VitalParameterStats]:
    """Between-site spread and digit statistics for each vital-sign parameter.

    Only treated subjects' non-null VSSTRESN at the tested sites enter the robust z.
    """
    treated = treated_subjects(dm).select("USUBJID", "SITEID")
    rows = (
        vs.filter(pl.col("VSTESTCD").is_in(parameters) & pl.col("VSSTRESN").is_not_null())
        .join(treated, on="USUBJID", how="inner")
        .select("SITEID", "VSTESTCD", "VSSTRESN")
    )
    out: list[VitalParameterStats] = []
    for parameter in parameters:
        sd: dict[str, float] = {}
        n: dict[str, int] = {}
        zero: dict[str, float] = {}
        for site in tested:
            values = rows.filter((pl.col("VSTESTCD") == parameter) & (pl.col("SITEID") == site))[
                "VSSTRESN"
            ].to_numpy()
            if len(values) < 2:
                continue
            site_sd = float(np.std(values, ddof=1))
            if site_sd <= 0:
                continue
            sd[site] = site_sd
            n[site] = len(values)
            ints = values[values == np.round(values)]
            if len(ints) >= min_readings:
                zero[site] = float(np.mean(np.abs(ints).astype(np.int64) % 10 == 0))
        log_sd = {s: math.log(v) for s, v in sd.items()}
        sd_z = robust_z(log_sd)
        zero_z = robust_z(zero)
        median_sd = float(np.median(list(sd.values()))) if sd else float("nan")
        sites = {
            s: VitalSiteStat(
                site=s,
                n=n[s],
                sd=sd[s],
                log_sd=log_sd[s],
                sd_ratio=sd[s] / median_sd,
                sd_z=None if sd_z is None else sd_z[s],
                zero_share=zero.get(s),
                zero_z=None if zero_z is None or s not in zero_z else zero_z[s],
            )
            for s in sorted(sd)
        }
        out.append(
            VitalParameterStats(
                parameter=parameter,
                sites=sites,
                sd_mad_zero=bool(sd) and sd_z is None,
                zero_mad_zero=bool(zero) and zero_z is None,
            )
        )
    return out
