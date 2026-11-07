"""The fault catalog. Each fault is a pure function (tables, spec, rng) -> (tables, record).

tables holds the blinded and ops tables of a cut by name (dm, ae, vs, ds, data_entry,
queries, deviations, ...). Unblinded tables are never passed in. Parameters are target
levels, not multipliers of the existing level, so each planted fault is detectable by
construction. Only treated subjects (RFSTDTC not blank) count.
"""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict

from meridian.data.derived import COMPLETED, DISPOSITION_EVENT, premature_discontinuation
from meridian.data.derived import treated_subjects as _treated
from meridian.domain.enums import FindingKind

STUDY_TARGET = "STUDY"
VITAL_TESTS: tuple[str, ...] = ("SYSBP", "DIABP", "PULSE")
WITHDRAWAL = "WITHDRAWAL BY SUBJECT"
MIN_OPEN_QUERIES = 5

Tables = dict[str, pl.DataFrame]


class FaultError(Exception):
    pass


class FaultSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fault_id: str
    fault_type: str
    target: str
    params: dict[str, float]


class FaultRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fault_id: str
    fault_type: str
    target: str
    params: dict[str, float]
    expected_kind: FindingKind
    kri: str | None
    rows_touched: dict[str, int]
    achieved: dict[str, float]


FaultFn = Callable[[Tables, FaultSpec, np.random.Generator], tuple[Tables, FaultRecord]]


@dataclass(frozen=True, slots=True)
class FaultType:
    name: str
    fn: FaultFn
    level: Literal["site", "study"]
    expected_kind: FindingKind
    kri: str | None
    params: tuple[str, ...]


REGISTRY: dict[str, FaultType] = {}


def register(
    name: str,
    *,
    level: Literal["site", "study"],
    expected_kind: FindingKind,
    kri: str | None,
    params: tuple[str, ...],
) -> Callable[[FaultFn], FaultFn]:
    def decorate(fn: FaultFn) -> FaultFn:
        if name in REGISTRY:
            raise ValueError(f"fault type {name!r} registered twice")
        REGISTRY[name] = FaultType(name, fn, level, expected_kind, kri, params)
        return fn

    return decorate


def _record(
    spec: FaultSpec, rows_touched: Mapping[str, int], achieved: Mapping[str, float]
) -> FaultRecord:
    kind = REGISTRY[spec.fault_type]
    return FaultRecord(
        fault_id=spec.fault_id,
        fault_type=spec.fault_type,
        target=spec.target,
        params=dict(spec.params),
        expected_kind=kind.expected_kind,
        kri=kind.kri,
        rows_touched=dict(rows_touched),
        achieved={k: float(v) for k, v in achieved.items()},
    )


def _site_treated(tables: Tables, site: str) -> list[str]:
    ids = _treated(tables["dm"]).filter(pl.col("SITEID") == site)["USUBJID"].to_list()
    if not ids:
        raise FaultError(f"site {site} has no treated subjects")
    return sorted(ids)


def _choose(rng: np.random.Generator, items: list[str], n: int) -> list[str]:
    if n > len(items):
        raise FaultError(f"need {n} items but only {len(items)} are available")
    picked = rng.choice(np.array(items, dtype=object), size=n, replace=False)
    return sorted(str(x) for x in picked)


def _num(value: object, default: float = 0.0) -> float:
    """A polars scalar statistic as float; None (empty input) becomes default."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FaultError(f"expected a number, got {value!r}")
    return float(value)


def _terminal_zero_share(values: pl.Series) -> float:
    ints = values.drop_nulls()
    return _num((ints % 10 == 0).mean()) if ints.len() else 0.0


@register(
    "fabricated_vitals",
    level="site",
    expected_kind=FindingKind.SITE_DATA_ANOMALY,
    kri=None,
    params=("subject_share", "sd_scale", "terminal_zero_share"),
)
def fabricated_vitals(
    tables: Tables, spec: FaultSpec, rng: np.random.Generator
) -> tuple[Tables, FaultRecord]:
    """Shrink chosen subjects' vitals towards the site mean, then round some to tens."""
    p = spec.params
    site_ids = _site_treated(tables, spec.target)
    chosen = _choose(rng, site_ids, max(1, round(p["subject_share"] * len(site_ids))))

    vs = tables["vs"].with_row_index("_row")
    in_scope = pl.col("VSTESTCD").is_in(VITAL_TESTS) & pl.col("VSSTRESN").is_not_null()
    means = dict(
        vs.filter(in_scope & pl.col("USUBJID").is_in(site_ids))
        .group_by("VSTESTCD")
        .agg(pl.col("VSSTRESN").mean())
        .iter_rows()
    )
    target = vs.filter(in_scope & pl.col("USUBJID").is_in(chosen)).sort("_row")
    rows = target["_row"].to_numpy()
    values = target["VSSTRESN"].to_numpy()
    mean = np.array([means[t] for t in target["VSTESTCD"]])
    new = np.rint(mean + p["sd_scale"] * (values - mean))
    n_round = round(p["terminal_zero_share"] * len(rows))
    to_round = rng.choice(len(rows), size=n_round, replace=False)
    new[to_round] = np.rint(new[to_round] / 10.0) * 10.0

    before = vs.filter(in_scope & pl.col("USUBJID").is_in(site_ids))
    patch = pl.DataFrame(
        {"_row": rows, "_new": new}, schema={"_row": pl.UInt32, "_new": pl.Float64}
    )
    out = (
        vs.join(patch, on="_row", how="left", maintain_order="left")
        .with_columns(
            pl.coalesce("_new", "VSSTRESN").alias("VSSTRESN"),
            pl.when(pl.col("_new").is_not_null())
            .then(pl.col("_new").cast(pl.Int64).cast(pl.String))
            .otherwise(pl.col("VSORRES"))
            .alias("VSORRES"),
            pl.when(pl.col("_new").is_not_null())
            .then(pl.col("_new").cast(pl.Int64).cast(pl.String))
            .otherwise(pl.col("VSSTRESC"))
            .alias("VSSTRESC"),
        )
        .drop("_row", "_new")
    )
    after = out.with_row_index("_row").filter(in_scope & pl.col("USUBJID").is_in(site_ids))
    achieved: dict[str, float] = {"subjects_changed": len(chosen)}
    for test in VITAL_TESTS:
        b = before.filter(pl.col("VSTESTCD") == test)["VSSTRESN"]
        a = after.filter(pl.col("VSTESTCD") == test)["VSSTRESN"]
        achieved[f"{test.lower()}_site_sd_before"] = _num(b.std())
        achieved[f"{test.lower()}_site_sd_after"] = _num(a.std())
        achieved[f"{test.lower()}_site_zero_share_before"] = _terminal_zero_share(b)
        achieved[f"{test.lower()}_site_zero_share_after"] = _terminal_zero_share(a)
    return {**tables, "vs": out}, _record(spec, {"vs": len(rows)}, achieved)


@register(
    "ae_under_reporting",
    level="site",
    expected_kind=FindingKind.UNDER_REPORTING,
    kri="ae_rate",
    params=("drop_share",),
)
def ae_under_reporting(
    tables: Tables, spec: FaultSpec, rng: np.random.Generator
) -> tuple[Tables, FaultRecord]:
    """Drop a share of the site's AE rows and renumber AESEQ so no gaps are left."""
    site_ids = _site_treated(tables, spec.target)
    ae = tables["ae"].with_row_index("_row")
    site_rows = ae.filter(pl.col("USUBJID").is_in(site_ids))["_row"].to_numpy()
    n_drop = round(spec.params["drop_share"] * len(site_rows))
    dropped = rng.choice(site_rows, size=n_drop, replace=False) if n_drop else np.array([])
    kept = (
        ae.filter(~pl.col("_row").is_in(dropped.tolist()))
        .with_columns(
            pl.col("AESEQ").rank("ordinal").over("USUBJID").cast(pl.Float64).alias("AESEQ")
        )
        .drop("_row")
    )
    achieved = {
        "site_ae_rows_before": len(site_rows),
        "site_ae_rows_after": len(site_rows) - n_drop,
        "dropped_share": n_drop / len(site_rows) if len(site_rows) else 0.0,
    }
    return {**tables, "ae": kept}, _record(spec, {"ae": n_drop}, achieved)


@register(
    "entry_lag_spike",
    level="site",
    expected_kind=FindingKind.KRI_BREACH,
    kri="data_entry_lag",
    params=("target_median_days",),
)
def entry_lag_spike(
    tables: Tables, spec: FaultSpec, rng: np.random.Generator
) -> tuple[Tables, FaultRecord]:
    """Scale the site's lags so the treated-subject median hits the target exactly.

    The per-site median and its z draw are rescaled consistently, so the synthetic
    model (lag = site_median * exp(sigma * z)) still reproduces every row.
    """
    del rng  # deterministic given the data
    site = spec.target
    site_ids = _site_treated(tables, site)
    de = tables["data_entry"]
    current = de.filter(pl.col("USUBJID").is_in(site_ids))["entry_lag_days"].median()
    if not isinstance(current, float) or current <= 0:
        raise FaultError(f"site {site} has no positive entry lag to scale")
    factor = spec.params["target_median_days"] / current

    # Sorted: polyfit's floating point result depends on row order.
    medians = (
        de.group_by("SITEID")
        .agg(pl.col("site_median_lag_days").first(), pl.col("site_median_z").first())
        .sort("SITEID")
    )
    others = medians.filter(pl.col("SITEID") != site)
    new_z: float | None = None
    if others["site_median_z"].n_unique() >= 2:
        slope, intercept = np.polyfit(
            others["site_median_z"].to_numpy(), np.log(others["site_median_lag_days"].to_numpy()), 1
        )
        old_median = float(medians.filter(pl.col("SITEID") == site)["site_median_lag_days"][0])
        new_z = float((math.log(old_median * factor) - intercept) / slope)

    is_site = pl.col("SITEID") == site
    out = de.with_columns(
        pl.when(is_site)
        .then(pl.col("entry_lag_days") * factor)
        .otherwise(pl.col("entry_lag_days"))
        .alias("entry_lag_days"),
        pl.when(is_site)
        .then(pl.col("site_median_lag_days") * factor)
        .otherwise(pl.col("site_median_lag_days"))
        .alias("site_median_lag_days"),
        pl.when(is_site & pl.lit(new_z is not None))
        .then(pl.lit(new_z, dtype=pl.Float64))
        .otherwise(pl.col("site_median_z"))
        .alias("site_median_z"),
    ).with_columns(
        (
            pl.col("SVSTDTC_DATE")
            + pl.duration(days=pl.col("entry_lag_days").ceil().cast(pl.Int64))
        ).alias("entered_date")
    )
    achieved_median = out.filter(pl.col("USUBJID").is_in(site_ids))["entry_lag_days"].median()
    touched = de.filter(is_site).height
    achieved = {
        "median_before": current,
        "median_after": float(achieved_median) if isinstance(achieved_median, float) else 0.0,
        "scale_factor": factor,
    }
    return {**tables, "data_entry": out}, _record(spec, {"data_entry": touched}, achieved)


@register(
    "query_backlog",
    level="site",
    expected_kind=FindingKind.KRI_BREACH,
    kri="query_aging",
    params=("target_median_age_days",),
)
def query_backlog(
    tables: Tables, spec: FaultSpec, rng: np.random.Generator
) -> tuple[Tables, FaultRecord]:
    """Age the site's open queries by a constant so their median hits the target.

    With fewer than 5 open queries (treated subjects), closed ones are reopened first.
    Reopened queries get a status draw below the largest open draw in the study and an
    age drawn like the other open queries, so status and draw stay consistent.
    """
    site = spec.target
    site_ids = _site_treated(tables, site)
    q = tables["queries"].with_row_index("_row")
    in_site = pl.col("USUBJID").is_in(site_ids) & (pl.col("SITEID") == site)
    open_site = q.filter(in_site & (pl.col("status") == "open"))
    flipped: list[int] = []
    if open_site.height < MIN_OPEN_QUERIES:
        closed = q.filter(in_site & (pl.col("status") == "closed"))["_row"].to_list()
        need = MIN_OPEN_QUERIES - open_site.height
        if need > len(closed):
            raise FaultError(f"site {site} has too few queries to build a backlog")
        flipped = sorted(int(x) for x in rng.choice(closed, size=need, replace=False))
        all_open = q.filter(pl.col("status") == "open")
        max_draw = _num(all_open["status_draw"].max(), 0.05)
        mean_age = _num(all_open["age_days"].mean(), 10.0)
        patch = pl.DataFrame(
            {
                "_row": flipped,
                "_draw": rng.uniform(0.0, max_draw, size=need),
                "_age": rng.exponential(mean_age, size=need),
            },
            schema={"_row": pl.UInt32, "_draw": pl.Float64, "_age": pl.Float64},
        )
        q = (
            q.join(patch, on="_row", how="left", maintain_order="left")
            .with_columns(
                pl.coalesce("_draw", "status_draw").alias("status_draw"),
                pl.when(pl.col("_draw").is_not_null())
                .then(pl.lit("open"))
                .otherwise(pl.col("status"))
                .alias("status"),
                pl.coalesce("_age", "age_days").alias("age_days"),
            )
            .drop("_draw", "_age")
        )
    before = q.filter(in_site & (pl.col("status") == "open"))["age_days"].median()
    assert isinstance(before, float)
    shift = max(spec.params["target_median_age_days"] - before, 0.0)
    is_open_site = (pl.col("SITEID") == site) & (pl.col("status") == "open")
    out = q.with_columns(
        pl.when(is_open_site)
        .then(pl.col("age_days") + shift)
        .otherwise(pl.col("age_days"))
        .alias("age_days")
    ).drop("_row")
    after = out.filter(
        pl.col("USUBJID").is_in(site_ids)
        & (pl.col("SITEID") == site)
        & (pl.col("status") == "open")
    )["age_days"]
    achieved = {
        "median_open_age_before": before,
        "median_open_age_after": _num(after.median()),
        "shift_days": shift,
        "reopened": len(flipped),
        "open_queries": after.len(),
    }
    touched = out.filter(is_open_site).height
    return {**tables, "queries": out}, _record(spec, {"queries": touched}, achieved)


@register(
    "deviation_cluster",
    level="site",
    expected_kind=FindingKind.KRI_BREACH,
    kri="deviations",
    params=("rate_multiplier",),
)
def deviation_cluster(
    tables: Tables, spec: FaultSpec, rng: np.random.Generator
) -> tuple[Tables, FaultRecord]:
    """Add deviations so the site's rate per treated subject is a multiple of the others'."""
    site = spec.target
    site_ids = _site_treated(tables, site)
    treated = _treated(tables["dm"])
    dev = tables["deviations"]
    dev_treated = dev.filter(pl.col("USUBJID").is_in(treated["USUBJID"].to_list()))
    other_subjects = treated.filter(pl.col("SITEID") != site).height
    other_devs = dev_treated.filter(pl.col("SITEID") != site).height
    if other_subjects == 0:
        raise FaultError("no other sites to compare against")
    other_rate = other_devs / other_subjects
    target_rate = spec.params["rate_multiplier"] * other_rate
    current = dev_treated.filter(pl.col("SITEID") == site).height
    target_count = math.ceil(target_rate * len(site_ids) - 1e-9)
    add = max(target_count - current, 0)

    # Category draws map to categories exactly as in the existing rows.
    pairs = dev.select("category_draw", "category").unique().sort("category_draw")
    if add and pairs.height == 0:
        raise FaultError("no existing deviations to learn categories from")
    subject_draw = rng.integers(0, len(site_ids), size=add)
    pair_index = rng.integers(0, max(pairs.height, 1), size=add)
    site_rows = dev.filter(pl.col("SITEID") == site)
    existing_n = site_rows.height
    template = dev.filter(pl.col("SITEID") == site).head(1)
    site_rate = float(template["site_rate"][0]) if template.height else other_rate
    site_expected = float(template["site_expected_count"][0]) if template.height else 0.0
    study_id = str(tables["dm"]["STUDYID"][0])
    src = dict(
        _treated(tables["dm"])
        .join(tables["dm"].select("USUBJID", "SRC_USUBJID"), on="USUBJID")
        .select("USUBJID", "SRC_USUBJID")
        .iter_rows()
    )
    new_rows = pl.DataFrame(
        {
            "STUDYID": [study_id] * add,
            "SITEID": [site] * add,
            "deviation_id": [f"DV-{site}-{existing_n + i + 1:04d}" for i in range(add)],
            "USUBJID": [site_ids[int(k)] for k in subject_draw],
            "SRC_USUBJID": [src[site_ids[int(k)]] for k in subject_draw],
            "category": [str(pairs["category"][int(k)]) for k in pair_index],
            "category_draw": [int(pairs["category_draw"][int(k)]) for k in pair_index],
            "subject_draw": [int(k) for k in subject_draw],
            "site_rate": [site_rate] * add,
            "site_expected_count": [site_expected] * add,
            "site_deviation_count": [0] * add,
        },
        schema=dev.schema,
    )
    out = (
        pl.concat([dev, new_rows], how="vertical")
        .with_columns(
            pl.when(pl.col("SITEID") == site)
            .then(pl.lit(existing_n + add, dtype=pl.Int64))
            .otherwise(pl.col("site_deviation_count"))
            .alias("site_deviation_count")
        )
        .sort("SITEID", "deviation_id", maintain_order=True)
    )
    achieved_count = out.filter(pl.col("USUBJID").is_in(site_ids)).height
    achieved = {
        "other_sites_rate": other_rate,
        "target_rate": target_rate,
        "achieved_rate": achieved_count / len(site_ids),
        "added": add,
    }
    return {**tables, "deviations": out}, _record(spec, {"deviations": add}, achieved)


@register(
    "discontinuation_drift",
    level="study",
    expected_kind=FindingKind.QTL_BREACH,
    kri=None,
    params=("target_rate",),
)
def discontinuation_drift(
    tables: Tables, spec: FaultSpec, rng: np.random.Generator
) -> tuple[Tables, FaultRecord]:
    """Turn completers into withdrawals until the study discontinuation rate hits target."""
    dm, ds = tables["dm"], tables["ds"]
    discontinued, treated = premature_discontinuation(dm, ds)
    needed = max(math.ceil(spec.params["target_rate"] * treated - 1e-9) - discontinued, 0)
    treated_ids = _treated(dm)["USUBJID"].to_list()
    completer_rows = ds.filter(
        (pl.col("DSCAT") == DISPOSITION_EVENT)
        & (pl.col("DSDECOD") == COMPLETED)
        & pl.col("USUBJID").is_in(treated_ids)
    )
    chosen = _choose(rng, sorted(set(completer_rows["USUBJID"].to_list())), needed)
    change = (
        (pl.col("DSCAT") == DISPOSITION_EVENT)
        & (pl.col("DSDECOD") == COMPLETED)
        & pl.col("USUBJID").is_in(chosen)
    )
    out = ds.with_columns(
        pl.when(change).then(pl.lit(WITHDRAWAL)).otherwise(pl.col("DSDECOD")).alias("DSDECOD"),
        pl.when(change).then(pl.lit(WITHDRAWAL)).otherwise(pl.col("DSTERM")).alias("DSTERM"),
    )
    after, _ = premature_discontinuation(dm, out)
    achieved = {
        "rate_before": discontinued / treated,
        "rate_after": after / treated,
        "subjects_changed": len(chosen),
    }
    touched = ds.filter(change).height
    return {**tables, "ds": out}, _record(spec, {"ds": touched}, achieved)
