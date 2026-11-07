"""Seeded synthesis of per-site operational data the pilot does not contain.

Randomness comes only from numpy Generators (PCG64). Each table gets its own child
of SeedSequence(seed), spawned in the fixed order of OPS_TABLES. Child i depends only on
(seed, i), so appending a new table later never changes the existing ones.

Every random draw ends up in a column of the output, and the seed record in the cut
manifest says which seed and spawn key produced each table.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated

import numpy as np
import polars as pl
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

OPS_TABLES: tuple[str, ...] = ("data_entry", "queries", "deviations", "screening")

PositiveFloat = Annotated[float, Field(gt=0)]
Probability = Annotated[float, Field(gt=0, lt=1)]


class OpsSynthParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    study_median_entry_lag_days: PositiveFloat = 3.0
    site_median_lag_sigma: PositiveFloat = 0.5
    entry_lag_sigma: PositiveFloat = 0.6
    queries_per_subject_visit: PositiveFloat = 0.4
    query_open_probability: Probability = 0.25
    open_query_age_mean_days: PositiveFloat = 10.0
    deviation_rate_mean: PositiveFloat = 0.15
    deviation_rate_shape: PositiveFloat = 4.0
    deviation_categories: Annotated[tuple[str, ...], Field(min_length=1)] = (
        "informed_consent",
        "eligibility",
        "visit_window",
        "procedure_not_done",
        "lab_sample_handling",
        "concomitant_medication",
    )
    screen_failure_rate_mean: Probability = 0.3
    screen_failure_concentration: PositiveFloat = 20.0


class SeedRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    bit_generator: str = "PCG64"
    spawn_order: tuple[str, ...]
    spawn_keys: dict[str, tuple[int, ...]]
    params: OpsSynthParams


@dataclass(frozen=True, slots=True)
class OpsData:
    tables: Mapping[str, pl.DataFrame]
    seed_record: SeedRecord


def spawn_generators(
    seed: int, order: Sequence[str] = OPS_TABLES
) -> tuple[dict[str, np.random.Generator], dict[str, tuple[int, ...]]]:
    children = np.random.SeedSequence(seed).spawn(len(order))
    generators = {
        name: np.random.Generator(np.random.PCG64(child))
        for name, child in zip(order, children, strict=True)
    }
    keys = {name: tuple(child.spawn_key) for name, child in zip(order, children, strict=True)}
    return generators, keys


def _subjects(dm: pl.DataFrame) -> pl.DataFrame:
    """Enrolled means a non-blank RFSTDTC (first study treatment). Uses blinded DM only."""
    return dm.select(
        "STUDYID",
        "SITEID",
        "USUBJID",
        "SRC_USUBJID",
        (pl.col("RFSTDTC").fill_null("").str.strip_chars() != "").alias("enrolled"),
    ).sort("USUBJID")


def _sites(subjects: pl.DataFrame) -> pl.DataFrame:
    return (
        subjects.group_by("SITEID")
        .agg(
            pl.len().alias("n_subjects"),
            pl.col("enrolled").sum().alias("n_enrolled"),
            (~pl.col("enrolled")).sum().alias("n_pilot_screen_failures"),
        )
        .sort("SITEID")
    )


def _visits(sv: pl.DataFrame, subjects: pl.DataFrame) -> pl.DataFrame:
    return (
        sv.select("USUBJID", "VISITNUM", "VISIT", "SVSTDTC", "SVSTDTC_DATE")
        .join(
            subjects.select("STUDYID", "SITEID", "USUBJID", "SRC_USUBJID"),
            on="USUBJID",
            how="left",
            maintain_order="left",
        )
        .select(
            "STUDYID",
            "SITEID",
            "USUBJID",
            "SRC_USUBJID",
            "VISITNUM",
            "VISIT",
            "SVSTDTC",
            "SVSTDTC_DATE",
        )
        .sort("USUBJID", "VISITNUM", "SVSTDTC", maintain_order=True)
    )


def _per_site(values: NDArray[np.float64], sites: pl.DataFrame, name: str) -> pl.DataFrame:
    return pl.DataFrame({"SITEID": sites["SITEID"], name: values})


def synth_data_entry(
    visits: pl.DataFrame, sites: pl.DataFrame, gen: np.random.Generator, p: OpsSynthParams
) -> pl.DataFrame:
    """entry_lag = site_median * exp(sigma * z); site_median = study_median * exp(s * z_site)."""
    site_z = gen.standard_normal(sites.height)
    site_median = p.study_median_entry_lag_days * np.exp(p.site_median_lag_sigma * site_z)
    site_frame = pl.DataFrame(
        {"SITEID": sites["SITEID"], "site_median_z": site_z, "site_median_lag_days": site_median}
    )
    frame = visits.join(site_frame, on="SITEID", how="left", maintain_order="left")
    entry_z = gen.standard_normal(frame.height)
    lag = frame["site_median_lag_days"].to_numpy() * np.exp(p.entry_lag_sigma * entry_z)
    return frame.with_columns(
        pl.Series("entry_lag_z", entry_z),
        pl.Series("entry_lag_days", lag),
    ).with_columns(
        (
            pl.col("SVSTDTC_DATE")
            + pl.duration(days=pl.col("entry_lag_days").ceil().cast(pl.Int64))
        ).alias("entered_date")
    )


def synth_queries(
    visits: pl.DataFrame, gen: np.random.Generator, p: OpsSynthParams
) -> pl.DataFrame:
    counts = gen.poisson(p.queries_per_subject_visit, visits.height)
    expanded = (
        visits.select("STUDYID", "SITEID", "USUBJID", "SRC_USUBJID", "VISITNUM", "VISIT")
        .with_columns(pl.Series("visit_query_count", counts, dtype=pl.Int64))
        .filter(pl.col("visit_query_count") > 0)
    )
    expanded = expanded.select(pl.all().repeat_by("visit_query_count").explode())
    n = expanded.height
    status_draw = gen.random(n)
    is_open = status_draw < p.query_open_probability
    ages = np.full(n, np.nan)
    ages[is_open] = gen.exponential(p.open_query_age_mean_days, int(is_open.sum()))
    return (
        expanded.with_columns(
            pl.int_range(1, pl.len() + 1).over("USUBJID", "VISITNUM").alias("query_number"),
            pl.int_range(1, pl.len() + 1).over("SITEID").alias("_site_n"),
            pl.Series("status_draw", status_draw),
            pl.Series("status", np.where(is_open, "open", "closed")),
            pl.Series("age_days", ages).fill_nan(None),
        )
        .with_columns(
            pl.concat_str(
                [pl.lit("Q"), pl.col("SITEID"), pl.col("_site_n").cast(pl.String).str.zfill(5)],
                separator="-",
            ).alias("query_id")
        )
        .drop("_site_n")
        .select(
            "STUDYID",
            "SITEID",
            "query_id",
            "USUBJID",
            "SRC_USUBJID",
            "VISITNUM",
            "VISIT",
            "visit_query_count",
            "query_number",
            "status_draw",
            "status",
            "age_days",
        )
    )


def synth_deviations(
    subjects: pl.DataFrame,
    sites: pl.DataFrame,
    gen: np.random.Generator,
    p: OpsSynthParams,
) -> pl.DataFrame:
    """Per site: rate ~ Gamma(shape, mean/shape) per enrolled subject; count ~ Poisson."""
    scale = p.deviation_rate_mean / p.deviation_rate_shape
    rate = gen.gamma(p.deviation_rate_shape, scale, sites.height)
    n_enrolled = sites["n_enrolled"].to_numpy()
    expected = rate * n_enrolled
    counts = gen.poisson(expected)
    site_index = np.repeat(np.arange(sites.height), counts)
    subject_draw = gen.integers(0, np.maximum(n_enrolled[site_index], 1)) if counts.sum() else []
    category_draw = gen.integers(0, len(p.deviation_categories), int(counts.sum()))

    enrolled = subjects.filter(pl.col("enrolled"))
    rows: list[dict[str, object]] = []
    for k, (i, s_idx, c_idx) in enumerate(
        zip(site_index, subject_draw, category_draw, strict=True)
    ):
        site_id = str(sites["SITEID"][int(i)])
        pool = enrolled.filter(pl.col("SITEID") == site_id)
        rows.append(
            {
                "SITEID": site_id,
                "_k": k,
                "USUBJID": pool["USUBJID"][int(s_idx)],
                "SRC_USUBJID": pool["SRC_USUBJID"][int(s_idx)],
                "subject_draw": int(s_idx),
                "category_draw": int(c_idx),
                "category": p.deviation_categories[int(c_idx)],
            }
        )
    schema = {
        "SITEID": pl.String,
        "_k": pl.Int64,
        "USUBJID": pl.String,
        "SRC_USUBJID": pl.String,
        "subject_draw": pl.Int64,
        "category_draw": pl.Int64,
        "category": pl.String,
    }
    site_params = pl.DataFrame(
        {
            "SITEID": sites["SITEID"],
            "site_rate": rate,
            "site_expected_count": expected,
            "site_deviation_count": counts.astype(np.int64),
        }
    )
    study_id = subjects["STUDYID"][0] if subjects.height else None
    return (
        pl.DataFrame(rows, schema=schema)
        .join(site_params, on="SITEID", how="left", maintain_order="left")
        .sort("_k")
        .with_columns(
            pl.lit(study_id, dtype=pl.String).alias("STUDYID"),
            pl.concat_str(
                [
                    pl.lit("DV"),
                    pl.col("SITEID"),
                    pl.int_range(1, pl.len() + 1).over("SITEID").cast(pl.String).str.zfill(4),
                ],
                separator="-",
            ).alias("deviation_id"),
        )
        .select(
            "STUDYID",
            "SITEID",
            "deviation_id",
            "USUBJID",
            "SRC_USUBJID",
            "category",
            "category_draw",
            "subject_draw",
            "site_rate",
            "site_expected_count",
            "site_deviation_count",
        )
    )


def synth_screening(
    subjects: pl.DataFrame,
    sites: pl.DataFrame,
    gen: np.random.Generator,
    p: OpsSynthParams,
) -> pl.DataFrame:
    """Screened = enrolled + pilot screen failures + synthetic screen failures.

    Per site: target rate r ~ Beta(mean * c, (1 - mean) * c). Target failures
    ~ NegativeBinomial(n_enrolled, 1 - r), the number of failures before n_enrolled
    enrolments. The pilot's own screen failures count toward the target; synthetic
    rows top it up, never below zero.
    """
    a = p.screen_failure_rate_mean * p.screen_failure_concentration
    b = (1 - p.screen_failure_rate_mean) * p.screen_failure_concentration
    rate = gen.beta(a, b, sites.height)
    n_enrolled = sites["n_enrolled"].to_numpy()
    target = gen.negative_binomial(np.maximum(n_enrolled, 1), 1 - rate)
    target = np.where(n_enrolled > 0, target, 0)
    pilot_failures = sites["n_pilot_screen_failures"].to_numpy()
    synthetic = np.maximum(target - pilot_failures, 0)

    site_params = pl.DataFrame(
        {
            "SITEID": sites["SITEID"],
            "site_sf_rate_draw": rate,
            "site_target_failures_draw": target.astype(np.int64),
            "site_pilot_failures": pilot_failures.astype(np.int64),
            "site_synthetic_failures": synthetic.astype(np.int64),
        }
    )
    real = subjects.select(
        "SITEID",
        "USUBJID",
        "SRC_USUBJID",
        pl.when(pl.col("enrolled"))
        .then(pl.lit("enrolled"))
        .otherwise(pl.lit("screen_failure"))
        .alias("outcome"),
        pl.lit("pilot").alias("source"),
    )
    fake = pl.DataFrame(
        {
            "SITEID": np.repeat(sites["SITEID"].to_numpy(), synthetic).tolist(),
        },
        schema={"SITEID": pl.String},
    ).with_columns(
        pl.lit(None, dtype=pl.String).alias("USUBJID"),
        pl.lit(None, dtype=pl.String).alias("SRC_USUBJID"),
        pl.lit("screen_failure").alias("outcome"),
        pl.lit("synthetic").alias("source"),
    )
    study_id = subjects["STUDYID"][0] if subjects.height else None
    return (
        pl.concat([real, fake], how="vertical")
        .with_columns(pl.col("source").replace_strict({"pilot": 0, "synthetic": 1}).alias("_src"))
        .sort("SITEID", "_src", "USUBJID", maintain_order=True, nulls_last=True)
        .with_columns(
            pl.lit(study_id, dtype=pl.String).alias("STUDYID"),
            pl.concat_str(
                [
                    pl.lit("SCR"),
                    pl.col("SITEID"),
                    pl.int_range(1, pl.len() + 1).over("SITEID").cast(pl.String).str.zfill(4),
                ],
                separator="-",
            ).alias("screening_id"),
        )
        .join(site_params, on="SITEID", how="left", maintain_order="left")
        .select(
            "STUDYID",
            "SITEID",
            "screening_id",
            "USUBJID",
            "SRC_USUBJID",
            "outcome",
            "source",
            "site_sf_rate_draw",
            "site_target_failures_draw",
            "site_pilot_failures",
            "site_synthetic_failures",
        )
    )


def synthesize(
    dm: pl.DataFrame,
    sv: pl.DataFrame,
    seed: int,
    params: OpsSynthParams,
    spawn_order: Sequence[str] = OPS_TABLES,
) -> OpsData:
    """Build every ops table from blinded DM and SV only.

    spawn_order must start with OPS_TABLES; extra trailing names reserve children for
    tables added later without disturbing these.
    """
    if tuple(spawn_order[: len(OPS_TABLES)]) != OPS_TABLES:
        raise ValueError(f"spawn_order must start with {OPS_TABLES}")
    gens, keys = spawn_generators(seed, spawn_order)
    subjects = _subjects(dm)
    sites = _sites(subjects)
    visits = _visits(sv, subjects)
    tables = {
        "data_entry": synth_data_entry(visits, sites, gens["data_entry"], params),
        "queries": synth_queries(visits, gens["queries"], params),
        "deviations": synth_deviations(subjects, sites, gens["deviations"], params),
        "screening": synth_screening(subjects, sites, gens["screening"], params),
    }
    record = SeedRecord(
        seed=seed,
        spawn_order=tuple(spawn_order),
        spawn_keys={name: keys[name] for name in OPS_TABLES},
        params=params,
    )
    return OpsData(tables=tables, seed_record=record)
