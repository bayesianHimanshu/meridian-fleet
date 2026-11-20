"""Small synthetic SEND studies from a written spec (phase 5 step 1): the stress set and the
agent's unit tests. Host-side test support only; no agent imports it.

A spec names the dose levels, animals per group, whether recovery groups exist, the
effects (a multiplier per dose group for a continuous endpoint, or affected counts for an
incidence one) and record-level edits (an outlier, a missing value, a unit change, an
early death). Values are drawn from a seeded generator, so a spec always builds the same
tables, byte for byte.
"""

from collections.abc import Sequence
from typing import Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

STUDY = "SYN-TOX-01"
SEXES = ("M", "F")
PHASES = ("main", "recovery")
MAIN_LAST_DAY = 92
RECOVERY_LAST_DAY = 106
BW_DAYS_MAIN = (1, 29, 57, 85, 92)
BW_DAYS_RECOVERY = (*BW_DAYS_MAIN, 99, 106)


class LbTest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    category: str
    unit: str
    mean_m: float
    mean_f: float
    cv: float


DEFAULT_LB: tuple[LbTest, ...] = (
    LbTest(code="ALT", category="CLINICAL CHEMISTRY", unit="U/L", mean_m=40, mean_f=32, cv=0.18),
    LbTest(code="AST", category="CLINICAL CHEMISTRY", unit="U/L", mean_m=90, mean_f=80, cv=0.18),
    LbTest(
        code="CREAT", category="CLINICAL CHEMISTRY", unit="mg/dL", mean_m=0.5, mean_f=0.55, cv=0.12
    ),
    LbTest(
        code="GLUC", category="CLINICAL CHEMISTRY", unit="mg/dL", mean_m=110, mean_f=105, cv=0.10
    ),
    LbTest(code="RBC", category="HEMATOLOGY", unit="10^12/L", mean_m=8.5, mean_f=8.0, cv=0.05),
    LbTest(code="HGB", category="HEMATOLOGY", unit="g/dL", mean_m=15.5, mean_f=15.0, cv=0.05),
)
ORGANS: dict[str, tuple[float, float, float]] = {  # mean male, mean female, cv (grams)
    "LIVER": (15.0, 9.0, 0.10),
    "KIDNEY": (3.2, 2.1, 0.09),
}
MI_SPECIMENS = ("LIVER", "KIDNEY")
BW_START = {"M": 300.0, "F": 200.0}
BW_FINAL = {"M": 560.0, "F": 310.0}
BW_CV = 0.07


class Effect(BaseModel):
    """factors: a multiplier per dose group (control first) for a continuous endpoint;
    counts: affected animals per group for an incidence endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: str  # lb:<code>, bw:GAIN, om:<organ>, mi:<specimen>:<finding>, death
    sex: Literal["M", "F", "both"] = "both"
    phase: Literal["main", "recovery", "both"] = "main"
    factors: tuple[float, ...] = ()
    counts: tuple[int, ...] = ()
    severity: str = "MILD"


class Edit(BaseModel):
    """A record-level edit to one animal (index within its group and sex, from 0)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["lb_factor", "lb_missing", "lb_unit", "death", "mi_finding"]
    sex: Literal["M", "F"]
    group: int  # dose rank: 0 is the control
    animal: int = 0
    phase: Literal["main", "recovery"] = "main"
    test: str = ""
    factor: float = 1.0
    value: float | None = None
    unit: str = ""
    day: int = 30
    animals: tuple[int, ...] = ()  # lb_missing / lb_unit: several animals at once
    # mi_finding, or death with a cause (1.1.0 stress cases): the animal's MI record of
    # this specimen gets this finding and MIRESCAT; for a death it is the cause (MIDTHREL Y).
    specimen: str = ""
    finding: str = ""
    category: str = ""


class StudySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int = 1
    doses: tuple[float, ...] = (0.0, 10.0, 30.0, 100.0)
    dose_unit: str = "mg/kg/day"
    n_main: int = 10
    recovery: bool = False
    n_recovery: int = 5
    effects: tuple[Effect, ...] = ()
    edits: tuple[Edit, ...] = ()
    lb: tuple[LbTest, ...] = DEFAULT_LB
    organs: dict[str, tuple[float, float, float]] = Field(default_factory=lambda: dict(ORGANS))


def _animals(spec: StudySpec) -> pl.DataFrame:
    rows = []
    phases = PHASES if spec.recovery else PHASES[:1]
    for phase in phases:
        n = spec.n_main if phase == "main" else spec.n_recovery
        for rank, _ in enumerate(spec.doses):
            setcd = f"{rank + 1}{'R' if phase == 'recovery' else ''}"
            for sex in SEXES:
                for i in range(n):
                    number = (
                        (rank + 1) * 1000
                        + (100 if phase == "recovery" else 0)
                        + (0 if sex == "M" else 50)
                        + i
                        + 1
                    )
                    rows.append(
                        {
                            "USUBJID": f"{STUDY}-{number}",
                            "SUBJID": str(number),
                            "SEX": sex,
                            "SETCD": setcd,
                            "ARMCD": setcd,
                            "rank": rank,
                            "phase": phase,
                            "index": i,
                        }
                    )
    return pl.DataFrame(rows)


def _applies(effect: Effect, sex: str, phase: str) -> bool:
    return effect.sex in (sex, "both") and effect.phase in (phase, "both")


def _factor(spec: StudySpec, endpoint: str, sex: str, phase: str, rank: int) -> float:
    f = 1.0
    for e in spec.effects:
        if e.endpoint == endpoint and _applies(e, sex, phase) and e.factors:
            f *= e.factors[rank]
    return f


def _design(spec: StudySpec, animals: pl.DataFrame) -> dict[str, pl.DataFrame]:
    ts = pl.DataFrame(
        {
            "STUDYID": [STUDY] * 4,
            "DOMAIN": ["TS"] * 4,
            "TSSEQ": [1.0, 2.0, 3.0, 4.0],
            "TSPARMCD": ["SSTYP", "SPECIES", "DOSDUR", "TRT"],
            "TSVAL": ["REPEAT DOSE TOXICITY", "RAT", "P13W", "SYNTH"],
        }
    )
    tx_rows = []
    for setcd in animals["SETCD"].unique(maintain_order=True).to_list():
        rank = int(setcd.rstrip("R")) - 1
        recovery = setcd.endswith("R")
        name = f"Group {rank + 1}, {spec.doses[rank]:g} {spec.dose_unit}, " + (
            "recovery" if recovery else "nonrecovery"
        )
        params = [
            ("ARMCD", setcd),
            ("TRTDOS", f"{spec.doses[rank]:g}"),
            ("TRTDOSU", spec.dose_unit),
            ("SPGRPCD", str(rank + 1)),
        ]
        if rank == 0:
            params.append(("TCNTRL", "Vehicle Control"))
        for seq, (code, value) in enumerate(params, start=1):
            tx_rows.append(
                {
                    "STUDYID": STUDY,
                    "DOMAIN": "TX",
                    "SETCD": setcd,
                    "SET": name,
                    "TXSEQ": float(seq),
                    "TXPARMCD": code,
                    "TXVAL": value,
                }
            )
    dm = animals.select(
        pl.lit(STUDY).alias("STUDYID"),
        pl.lit("DM").alias("DOMAIN"),
        "USUBJID",
        "SUBJID",
        "SEX",
        "ARMCD",
        pl.col("SETCD").alias("ARM"),
        "SETCD",
    )
    return {"ts": ts, "tx": pl.DataFrame(tx_rows), "dm": dm}


def build(spec: StudySpec) -> dict[str, pl.DataFrame]:
    """The SEND tables of a spec: ts tx dm ds bw lb om mi ma cl."""
    rng = np.random.default_rng(spec.seed)
    animals = _animals(spec)
    tables = _design(spec, animals)
    deaths: dict[tuple[str, int, str, int], int] = {
        (e.sex, e.group, e.phase, e.animal): e.day for e in spec.edits if e.kind == "death"
    }
    for e in spec.effects:  # dose-related deaths: the first animals of each group die
        if e.endpoint == "death":
            for sex in SEXES:
                for phase in PHASES:
                    if _applies(e, sex, phase):
                        for rank, count in enumerate(e.counts):
                            for i in range(count):
                                deaths.setdefault((sex, rank, phase, i), 40 + 5 * i)
    ds, bw, lb, om, mi = [], [], [], [], []
    for a in animals.iter_rows(named=True):
        sex, rank, phase, i, uid = a["SEX"], a["rank"], a["phase"], a["index"], a["USUBJID"]
        death = deaths.get((sex, rank, phase, i))
        last = (
            death
            if death is not None
            else (MAIN_LAST_DAY if phase == "main" else RECOVERY_LAST_DAY)
        )
        decod = (
            "MORIBUND SACRIFICE"
            if death is not None
            else "TERMINAL SACRIFICE"
            if phase == "main"
            else "RECOVERY SACRIFICE"
        )
        ds.append({"USUBJID": uid, "DSSEQ": 1.0, "DSDECOD": decod, "DSSTDY": float(last)})
        # Body weight: a straight line from start to final, the gain scaled by its factor.
        start = BW_START[sex] * (1 + rng.normal(0, BW_CV))
        gain = (BW_FINAL[sex] - BW_START[sex]) * (1 + rng.normal(0, BW_CV * 2))
        gain *= _factor(spec, "bw:GAIN", sex, phase, rank)
        days = BW_DAYS_MAIN if phase == "main" else BW_DAYS_RECOVERY
        weight = start
        for seq, day in enumerate((d for d in days if d <= last), start=1):
            weight = start + gain * min(day, MAIN_LAST_DAY) / MAIN_LAST_DAY
            bw.append(
                {
                    "USUBJID": uid,
                    "BWSEQ": float(seq),
                    "BWTESTCD": "BW",
                    "BWSTRESN": round(weight, 1),
                    "BWSTRESU": "g",
                    "BWDY": float(day),
                }
            )
        bw.append(
            {
                "USUBJID": uid,
                "BWSEQ": 99.0,
                "BWTESTCD": "TERMBW",
                "BWSTRESN": round(weight * 0.95, 1),
                "BWSTRESU": "g",
                "BWDY": float(last),
            }
        )
        # Clinical pathology at the scheduled day only (none for an early death).
        for seq, t in enumerate(spec.lb, start=1):
            mean = t.mean_m if sex == "M" else t.mean_f
            value = (
                mean * (1 + rng.normal(0, t.cv)) * _factor(spec, f"lb:{t.code}", sex, phase, rank)
            )
            if death is None:
                lb.append(
                    {
                        "USUBJID": uid,
                        "LBSEQ": float(seq),
                        "LBTESTCD": t.code,
                        "LBCAT": t.category,
                        "LBSTRESN": round(value, 3),
                        "LBSTRESU": t.unit,
                        "LBSTAT": "",
                        "LBDY": float(last),
                    }
                )
        if death is None:
            for seq, (organ, (mm, mf, cv)) in enumerate(sorted(spec.organs.items()), start=1):
                value = (mm if sex == "M" else mf) * (1 + rng.normal(0, cv))
                value *= _factor(spec, f"om:{organ}", sex, phase, rank)
                om.append(
                    {
                        "USUBJID": uid,
                        "OMSEQ": float(seq),
                        "OMTESTCD": "WEIGHT",
                        "OMSPEC": organ,
                        "OMSTRESN": round(value, 3),
                        "OMSTRESU": "g",
                    }
                )
        for seq, specimen in enumerate(MI_SPECIMENS, start=1):
            result, severity, category, related = "NORMAL", "", "", ""
            for e in spec.effects:
                if (
                    e.endpoint.startswith(f"mi:{specimen}:")
                    and _applies(e, sex, phase)
                    and e.counts
                    and i < e.counts[rank]
                ):
                    result, severity = e.endpoint.split(":", 2)[2], e.severity
            for ed in spec.edits:
                if (
                    ed.kind in ("mi_finding", "death")
                    and ed.finding
                    and ed.specimen == specimen
                    and (ed.sex, ed.group, ed.phase, ed.animal) == (sex, rank, phase, i)
                ):
                    result, severity, category = ed.finding, "MARKED", ed.category
                    related = "Y" if ed.kind == "death" else "N"
            mi.append(
                {
                    "USUBJID": uid,
                    "MISEQ": float(seq),
                    "MISPEC": specimen,
                    "MISTRESC": result,
                    "MISEV": severity,
                    "MISTAT": "",
                    "MIRESCAT": category,
                    "MIDTHREL": related,
                }
            )
    lbf = pl.DataFrame(lb)
    for edit in spec.edits:
        lbf = _edit(lbf, animals, edit)
    tables |= {
        "ds": pl.DataFrame(ds),
        "bw": pl.DataFrame(bw),
        "lb": lbf,
        "om": pl.DataFrame(om),
        "mi": pl.DataFrame(mi),
        "ma": pl.DataFrame(
            schema={
                "USUBJID": pl.String,
                "MASEQ": pl.Float64,
                "MASPEC": pl.String,
                "MASTRESC": pl.String,
            }
        ),
        "cl": pl.DataFrame(
            schema={
                "USUBJID": pl.String,
                "CLSEQ": pl.Float64,
                "CLCAT": pl.String,
                "CLSTRESC": pl.String,
                "CLDY": pl.Float64,
            }
        ),
    }
    for name in ("ds", "bw", "lb", "om", "mi", "ma", "cl"):
        frame = tables[name]
        tables[name] = frame.select(
            pl.lit(STUDY, dtype=pl.String).alias("STUDYID"),
            pl.lit(name.upper(), dtype=pl.String).alias("DOMAIN"),
            pl.all(),
        )
    return {k: tables[k] for k in sorted(tables)}


def _uids(animals: pl.DataFrame, e: Edit, indexes: Sequence[int]) -> list[str]:
    return animals.filter(
        (pl.col("SEX") == e.sex)
        & (pl.col("rank") == e.group)
        & (pl.col("phase") == e.phase)
        & pl.col("index").is_in(list(indexes))
    )["USUBJID"].to_list()


def _edit(lb: pl.DataFrame, animals: pl.DataFrame, e: Edit) -> pl.DataFrame:
    if e.kind in ("death", "mi_finding"):
        return lb
    which = e.animals or (e.animal,)
    uids = _uids(animals, e, which)
    target = pl.col("USUBJID").is_in(uids) & (pl.col("LBTESTCD") == e.test)
    if e.kind == "lb_factor":
        if e.value is not None:
            return lb.with_columns(
                pl.when(target)
                .then(pl.lit(e.value))
                .otherwise(pl.col("LBSTRESN"))
                .alias("LBSTRESN")
            )
        return lb.with_columns(
            pl.when(target).then(pl.col("LBSTRESN") * e.factor).otherwise(pl.col("LBSTRESN"))
        )
    if e.kind == "lb_missing":
        return lb.with_columns(
            pl.when(target).then(None).otherwise(pl.col("LBSTRESN")).alias("LBSTRESN"),
            pl.when(target).then(pl.lit("NOT DONE")).otherwise(pl.col("LBSTAT")).alias("LBSTAT"),
        )
    # lb_unit: the values recorded in another unit (value x factor, unit as given)
    return lb.with_columns(
        pl.when(target)
        .then(pl.col("LBSTRESN") * e.factor)
        .otherwise(pl.col("LBSTRESN"))
        .alias("LBSTRESN"),
        pl.when(target).then(pl.lit(e.unit)).otherwise(pl.col("LBSTRESU")).alias("LBSTRESU"),
    )
