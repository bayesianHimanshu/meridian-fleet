"""A repeat-dose study, analysed as pre-registered (docs/preclinical-eval.md).

read_design turns TX, DM and DS into groups (dose rank within phase, control, recovery,
TK) and animals. endpoint_values gives one value per animal and continuous endpoint, and
one affected or not per animal and incidence endpoint, with the records it could not use
(Problem). analyse groups them by endpoint, sex and phase, and computes the group table
and the tests; preclinical.tox_study_monitor turns that into findings, and the Fleet API
shows the same tables. Pure: tables in, results out; nothing is read from disk here.
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

import polars as pl

from meridian.platform.agent_settings import ToxStudyMonitorConfig
from meridian.preclinical.stats import cochran_armitage_exact, dunnett, fisher_greater, jonckheere

MAIN, RECOVERY = "main", "recovery"
SEXES = ("M", "F")
SCHEDULED = frozenset({"TERMINAL SACRIFICE", "RECOVERY SACRIFICE"})
NOT_FINDINGS = frozenset({"", "NORMAL", "UNREMARKABLE", "NO VISIBLE LESIONS"})
SEVERITY_GRADE = {"MINIMAL": 1, "MILD": 2, "MODERATE": 3, "MARKED": 4, "SEVERE": 5}
LB_CLASS = {
    "HEMATOLOGY": "hematology",
    "COAGULATION": "coagulation",
    "CLINICAL CHEMISTRY": "clinical_chemistry",
    "URINALYSIS": "urinalysis",
}
_RECOVERY = re.compile(r"(?<![a-z])recovery")
# 1.1.0: an MI finding is a neoplasm when MIRESCAT says BENIGN or MALIGNANT; where MIRESCAT
# is empty for every record of it, when its term names one.
NEOPLASTIC_CATEGORIES = frozenset({"BENIGN", "MALIGNANT"})
NEOPLASM_TERM = re.compile(
    r"\b\w*(?:CARCINOMA|SARCOMA|ADENOMA|LYMPHOMA|LEUKEMIA|PAPILLOMA|FIBROMA|LIPOMA|"
    r"LEIOMYOMA|THYMOMA|MESOTHELIOMA|PHEOCHROMOCYTOMA|NEOPLASM|TUMOR)\b"
)

# The finding checks (docs/preclinical-eval.md).
DOSE_RELATED_CHANGE = "dose_related_change"
PAIRWISE_CHANGE = "pairwise_change"
DOSE_RELATED_INCIDENCE = "dose_related_incidence"
PAIRWISE_INCIDENCE = "pairwise_incidence"
DATA_QUALITY = "data_quality"


@dataclass(frozen=True, slots=True)
class Problem:
    """Records the agent could not use: kind (unit_mismatch, missing_unit, duplicate,
    unknown_set, unknown_sex, bad_dose, control_ambiguous, control_too_few), the endpoint
    or thing it concerns, and how many records."""

    kind: str
    detail: str
    domain: str
    count: int
    example: str = ""

    @property
    def check(self) -> str:
        return f"{DATA_QUALITY}:{self.kind}:{self.detail}"


@dataclass(frozen=True, slots=True)
class Group:
    setcd: str
    phase: str
    rank: int
    dose: float
    control: bool
    label: str


@dataclass(frozen=True, slots=True)
class Design:
    groups: tuple[Group, ...]  # analysed sets only (no TK, no ambiguous phase)
    animals: pl.DataFrame  # USUBJID SEX SETCD phase rank dose unscheduled death_day
    dose_unit: str
    problems: tuple[Problem, ...]

    def doses(self, phase: str) -> list[float]:
        return sorted({g.dose for g in self.groups if g.phase == phase})


def _tx_params(tx: pl.DataFrame) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in tx.sort(["SETCD", "TXSEQ"], maintain_order=True).iter_rows(named=True):
        params = out.setdefault(str(row["SETCD"]), {"SET": str(row.get("SET") or "")})
        params.setdefault(str(row["TXPARMCD"]).upper(), str(row["TXVAL"] or ""))
    return out


def read_design(dm: pl.DataFrame, tx: pl.DataFrame, ds: pl.DataFrame | None) -> Design:
    problems: list[Problem] = []
    params = _tx_params(tx)
    sets: list[tuple[str, str, float, bool, str]] = []  # setcd, phase, dose, control, label
    units = Counter[str]()
    for setcd, p in sorted(params.items()):
        if p.get("TKDESC", "").strip().upper() == "TK":
            continue
        name = p.get("SET", "") or p.get("SETLBL", "")
        recovery = "RECOVDUR" in p or bool(_RECOVERY.search(name.lower()))
        try:
            dose = float(p.get("TRTDOS", ""))
        except ValueError:
            problems.append(Problem("bad_dose", f"set {setcd}", "tx", 1, p.get("TRTDOS", "")))
            continue
        units[p.get("TRTDOSU", "")] += 1
        control = "TCNTRL" in p and dose == 0
        sets.append((setcd, RECOVERY if recovery else MAIN, dose, control, p.get("TCNTRL", "")))
    groups: list[Group] = []
    for phase in (MAIN, RECOVERY):
        mine = [s for s in sets if s[1] == phase]
        controls = [s for s in mine if s[3]]
        if len(controls) > 1:
            controls = [s for s in controls if "vehicle" in s[4].lower()]
        if mine and len(controls) != 1:
            problems.append(Problem("control_ambiguous", phase, "tx", len(controls)))
            continue
        keep = [s for s in mine if not s[3] or s in controls]
        doses = sorted({s[2] for s in keep})
        for setcd, _, dose, control, _ in keep:
            groups.append(Group(setcd, phase, doses.index(dose), dose, control, f"{dose:g}"))
    by_set = {g.setcd: g for g in groups}
    known = set(params)
    unknown = dm.filter(~pl.col("SETCD").is_in(sorted(known)))
    if unknown.height:
        problems.append(Problem("unknown_set", "dm", "dm", unknown.height, unknown["SETCD"][0]))
    bad_sex = dm.filter(~pl.col("SEX").is_in(SEXES))
    if bad_sex.height:
        problems.append(Problem("unknown_sex", "dm", "dm", bad_sex.height, str(bad_sex["SEX"][0])))
    rows = []
    for a in dm.filter(pl.col("SEX").is_in(SEXES)).iter_rows(named=True):
        g = by_set.get(str(a["SETCD"]))
        if g is not None:
            rows.append(
                {
                    "USUBJID": a["USUBJID"],
                    "SEX": a["SEX"],
                    "SETCD": g.setcd,
                    "phase": g.phase,
                    "rank": g.rank,
                    "dose": g.dose,
                }
            )
    animals = pl.DataFrame(
        rows,
        schema={
            "USUBJID": pl.String,
            "SEX": pl.String,
            "SETCD": pl.String,
            "phase": pl.String,
            "rank": pl.Int64,
            "dose": pl.Float64,
        },
    )
    deaths = pl.DataFrame(
        schema={"USUBJID": pl.String, "unscheduled": pl.Boolean, "death_day": pl.Float64}
    )
    if ds is not None and ds.height:
        deaths = (
            ds.sort(["USUBJID", "DSSTDY"], maintain_order=True, nulls_last=True)
            .group_by("USUBJID", maintain_order=True)
            .agg(
                (~pl.col("DSDECOD").str.to_uppercase().is_in(sorted(SCHEDULED)))
                .any()
                .alias("unscheduled"),
                pl.col("DSSTDY").cast(pl.Float64).max().alias("death_day"),
            )
        )
    animals = animals.join(deaths, on="USUBJID", how="left").with_columns(
        pl.col("unscheduled").fill_null(False)
    )
    unit = units.most_common(1)[0][0] if units else ""
    return Design(tuple(groups), animals.sort("USUBJID"), unit, tuple(problems))


# ------------------------------------------------------------------ endpoint values


@dataclass(frozen=True, slots=True)
class Endpoint:
    endpoint: str  # lb:ALT, bw:GAIN, om:LIVER:rel, mi:LIVER:HYPERTROPHY, death
    label: str
    kind: str  # continuous or incidence
    cls: str  # magnitude class (continuous) or domain (incidence)
    unit: str = ""
    neoplastic: bool = False  # 1.1.0: an MI neoplasm (its own rule, never an incidence finding)


@dataclass(frozen=True, slots=True)
class Values:
    endpoints: dict[str, Endpoint]
    continuous: pl.DataFrame  # endpoint USUBJID value
    incidence: pl.DataFrame  # endpoint USUBJID affected grade (examined animals only)
    problems: tuple[Problem, ...]


def _mode(values: list[float]) -> float:
    counts = Counter(values)
    top = max(counts.values())
    return max(v for v, c in counts.items() if c == top)


def _table(tables: Mapping[str, pl.DataFrame], name: str) -> pl.DataFrame | None:
    frame = tables.get(name)
    return frame if frame is not None and frame.height else None


def _str(frame: pl.DataFrame, col: str) -> pl.Expr:
    if col in frame.columns:
        return pl.col(col).cast(pl.String).fill_null("").str.strip_chars().str.to_uppercase()
    return pl.lit("", dtype=pl.String)


def _day(frame: pl.DataFrame, prefix: str) -> pl.Expr:
    for col in (f"{prefix}DY", "VISITDY"):
        if col in frame.columns:
            return pl.col(col).cast(pl.Float64)
    return pl.lit(None, dtype=pl.Float64)


def _bw(bw: pl.DataFrame, animals: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, Endpoint]]:
    rows = (
        bw.with_columns(
            test=_str(bw, "BWTESTCD"), day=_day(bw, "BW"), value=pl.col("BWSTRESN").cast(pl.Float64)
        )
        .filter(
            (pl.col("test") == "BW") & pl.col("value").is_not_null() & pl.col("day").is_not_null()
        )
        .join(animals.select("USUBJID", "phase"), on="USUBJID")
    )
    out = []
    for phase in (MAIN, RECOVERY):
        mine = rows.filter(pl.col("phase") == phase)
        if not mine.height:
            continue
        last = (
            mine.group_by("USUBJID", maintain_order=True).agg(pl.col("day").max())["day"].to_list()
        )
        final_day = _mode(last)
        first = mine.sort(["USUBJID", "day"]).group_by("USUBJID", maintain_order=True).first()
        final = (
            mine.filter(pl.col("day") == final_day)
            .group_by("USUBJID", maintain_order=True)
            .agg(pl.col("value").mean().alias("final"))
        )
        both = final.join(first.select("USUBJID", pl.col("value").alias("first")), on="USUBJID")
        out.append(
            both.select(
                "USUBJID", pl.lit("bw:BW").alias("endpoint"), pl.col("final").alias("value")
            )
        )
        out.append(
            both.select(
                "USUBJID",
                pl.lit("bw:GAIN").alias("endpoint"),
                (pl.col("final") - pl.col("first")).alias("value"),
            )
        )
    eps = {
        "bw:BW": Endpoint("bw:BW", "Body weight", "continuous", "body_weight", "g"),
        "bw:GAIN": Endpoint("bw:GAIN", "Body weight gain", "continuous", "body_weight_gain", "g"),
    }
    return (pl.concat(out) if out else _EMPTY_CONT), eps


_EMPTY_CONT = pl.DataFrame(
    schema={"USUBJID": pl.String, "endpoint": pl.String, "value": pl.Float64}
)
_EMPTY_INC = pl.DataFrame(
    schema={"endpoint": pl.String, "USUBJID": pl.String, "affected": pl.Boolean, "grade": pl.Int64}
)


def _lb(
    lb: pl.DataFrame, animals: pl.DataFrame
) -> tuple[pl.DataFrame, dict[str, Endpoint], list[Problem]]:
    problems: list[Problem] = []
    rows = (
        lb.with_columns(
            test=_str(lb, "LBTESTCD"),
            cat=_str(lb, "LBCAT"),
            spec=_str(lb, "LBSPEC"),
            unit=pl.col("LBSTRESU").cast(pl.String).fill_null("").str.strip_chars()
            if "LBSTRESU" in lb.columns
            else pl.lit(""),
            stat=_str(lb, "LBSTAT"),
            day=_day(lb, "LB"),
            value=pl.col("LBSTRESN").cast(pl.Float64),
        )
        .filter(pl.col("value").is_not_null() & (pl.col("stat") != "NOT DONE"))
        .join(animals.select("USUBJID", "phase"), on="USUBJID")
    )
    specs = rows.group_by("test", maintain_order=True).agg(pl.col("spec").n_unique().alias("n"))
    multi = set(specs.filter(pl.col("n") > 1)["test"].to_list())
    rows = rows.with_columns(
        endpoint=pl.when(pl.col("test").is_in(sorted(multi)))
        .then(pl.concat_str(pl.lit("lb:"), "test", pl.lit(":"), "spec"))
        .otherwise(pl.concat_str(pl.lit("lb:"), "test"))
    )
    eps: dict[str, Endpoint] = {}
    kept = []
    for endpoint in sorted(rows["endpoint"].unique().to_list()):
        mine = rows.filter(pl.col("endpoint") == endpoint)
        unit = Counter(mine["unit"].to_list()).most_common(1)[0][0]
        cat = Counter(mine["cat"].to_list()).most_common(1)[0][0]
        bad = mine.filter(pl.col("unit") != unit)
        for kind, part in (
            ("unit_mismatch", bad.filter(pl.col("unit") != "")),
            ("missing_unit", bad.filter(pl.col("unit") == "")),
        ):
            if part.height:
                problems.append(
                    Problem(
                        kind,
                        endpoint,
                        "lb",
                        part.height,
                        f"{part['USUBJID'][0]} {part['unit'][0] or 'empty'}",
                    )
                )
        mine = mine.filter(pl.col("unit") == unit)
        scheduled = []
        for phase in (MAIN, RECOVERY):
            ph = mine.filter(pl.col("phase") == phase)
            if ph.height:
                scheduled.append(
                    ph.filter(pl.col("day") == _mode(ph["day"].drop_nulls().to_list()))
                    if ph["day"].drop_nulls().len()
                    else ph
                )
        if not scheduled:
            continue
        sched = pl.concat(scheduled)
        dup = sched.group_by("USUBJID", maintain_order=True).len().filter(pl.col("len") > 1)
        if dup.height:
            problems.append(
                Problem("duplicate", endpoint, "lb", int(dup["len"].sum()), dup["USUBJID"][0])
            )
            sched = sched.filter(~pl.col("USUBJID").is_in(dup["USUBJID"].to_list()))
        name = endpoint.removeprefix("lb:").replace(":", " in ").title()
        label = mine["test"][0] if ":" not in endpoint.removeprefix("lb:") else name
        eps[endpoint] = Endpoint(endpoint, label, "continuous", LB_CLASS.get(cat, "other"), unit)
        kept.append(sched.select("USUBJID", "endpoint", "value"))
    return (pl.concat(kept) if kept else _EMPTY_CONT), eps, problems


def _om(
    om: pl.DataFrame, bw: pl.DataFrame | None, animals: pl.DataFrame
) -> tuple[pl.DataFrame, dict[str, Endpoint], list[Problem]]:
    problems: list[Problem] = []
    rows = (
        om.with_columns(
            test=_str(om, "OMTESTCD"),
            spec=_str(om, "OMSPEC"),
            unit=pl.col("OMSTRESU").cast(pl.String).fill_null("")
            if "OMSTRESU" in om.columns
            else pl.lit(""),
            value=pl.col("OMSTRESN").cast(pl.Float64),
        )
        .filter((pl.col("test") == "WEIGHT") & pl.col("value").is_not_null())
        .join(animals.select("USUBJID"), on="USUBJID")
    )
    term = _EMPTY_CONT.select("USUBJID", pl.col("value").alias("termbw"))
    if bw is not None:
        term = (
            bw.with_columns(
                test=_str(bw, "BWTESTCD"),
                day=_day(bw, "BW"),
                termbw=pl.col("BWSTRESN").cast(pl.Float64),
            )
            .filter((pl.col("test") == "TERMBW") & pl.col("termbw").is_not_null())
            .sort(["USUBJID", "day"], nulls_last=True)
            .group_by("USUBJID", maintain_order=True)
            .last()
            .select("USUBJID", "termbw")
        )
    eps: dict[str, Endpoint] = {}
    out = []
    for spec in sorted(rows["spec"].unique().to_list()):
        mine = rows.filter(pl.col("spec") == spec)
        unit = Counter(mine["unit"].to_list()).most_common(1)[0][0]
        bad = mine.filter(pl.col("unit") != unit)
        if bad.height:
            problems.append(
                Problem("unit_mismatch", f"om:{spec}", "om", bad.height, bad["USUBJID"][0])
            )
        mine = mine.filter(pl.col("unit") == unit)
        dup = mine.group_by("USUBJID", maintain_order=True).len().filter(pl.col("len") > 1)
        if dup.height:
            problems.append(
                Problem("duplicate", f"om:{spec}", "om", int(dup["len"].sum()), dup["USUBJID"][0])
            )
            mine = mine.filter(~pl.col("USUBJID").is_in(dup["USUBJID"].to_list()))
        name = spec.title()
        eps[f"om:{spec}:abs"] = Endpoint(
            f"om:{spec}:abs", f"{name} weight", "continuous", "organ_weight", unit
        )
        eps[f"om:{spec}:rel"] = Endpoint(
            f"om:{spec}:rel",
            f"{name} weight relative to body weight",
            "continuous",
            "organ_weight",
            "%",
        )
        out.append(mine.select("USUBJID", pl.lit(f"om:{spec}:abs").alias("endpoint"), "value"))
        rel = mine.join(term, on="USUBJID").filter(pl.col("termbw") > 0)
        out.append(
            rel.select(
                "USUBJID",
                pl.lit(f"om:{spec}:rel").alias("endpoint"),
                (pl.col("value") / pl.col("termbw") * 100).alias("value"),
            )
        )
    return (pl.concat(out) if out else _EMPTY_CONT), eps, problems


def _findings_domain(
    frame: pl.DataFrame,
    domain: str,
    animals: pl.DataFrame,
    *,
    examined_by_record: bool,
    category: str | None = None,
) -> tuple[pl.DataFrame, dict[str, Endpoint]]:
    p = domain.upper()
    spec_col = f"{p}SPEC" if f"{p}SPEC" in frame.columns else None
    rows = (
        frame.with_columns(
            spec=_str(frame, spec_col) if spec_col else _str(frame, category or "CLCAT"),
            result=_str(frame, f"{p}STRESC"),
            stat=_str(frame, f"{p}STAT"),
            sev=_str(frame, f"{p}SEV"),
            cat=_str(frame, f"{p}RESCAT"),
        )
        .filter(pl.col("stat") != "NOT DONE")
        .join(animals.select("USUBJID"), on="USUBJID")
    )
    eps: dict[str, Endpoint] = {}
    out = []
    names = {"mi": "microscopic", "ma": "macroscopic", "cl": "clinical observation"}
    for (spec, result), part in sorted(
        rows.filter(~pl.col("result").is_in(sorted(NOT_FINDINGS))).group_by(
            ["spec", "result"], maintain_order=True
        ),
        key=lambda kv: (str(kv[0][0]), str(kv[0][1])),
    ):
        endpoint = f"{domain}:{spec}:{result}"
        if examined_by_record:
            examined = rows.filter(pl.col("spec") == spec)["USUBJID"].unique()
        else:
            examined = animals["USUBJID"]
        grades = (
            part.with_columns(
                grade=pl.col("sev").replace_strict(
                    SEVERITY_GRADE, default=None, return_dtype=pl.Int64
                )
            )
            .group_by("USUBJID", maintain_order=True)
            .agg(pl.col("grade").max())
        )
        frame_ex = pl.DataFrame({"USUBJID": examined.to_list()}, schema={"USUBJID": pl.String})
        out.append(
            frame_ex.join(grades, on="USUBJID", how="left").select(
                pl.lit(endpoint).alias("endpoint"),
                "USUBJID",
                pl.col("USUBJID").is_in(part["USUBJID"].to_list()).alias("affected"),
                pl.col("grade"),
            )
        )
        what = f"{str(spec).title()}: {str(result).lower()}"
        cats = set(part["cat"].to_list()) - {""}
        neoplastic = domain == "mi" and (
            bool(cats & NEOPLASTIC_CATEGORIES) if cats else bool(NEOPLASM_TERM.search(str(result)))
        )
        eps[endpoint] = Endpoint(
            endpoint, f"{what} ({names[domain]})", "incidence", domain, neoplastic=neoplastic
        )
    return (pl.concat(out) if out else _EMPTY_INC), eps


def endpoint_values(tables: Mapping[str, pl.DataFrame], design: Design) -> Values:
    animals = design.animals
    cont, inc = [_EMPTY_CONT.select("endpoint", "USUBJID", "value")], [_EMPTY_INC]
    eps: dict[str, Endpoint] = {}
    problems: list[Problem] = []
    bw = _table(tables, "bw")
    if bw is not None:
        frame, e = _bw(bw, animals)
        cont.append(frame.select("endpoint", "USUBJID", "value"))
        eps |= e
    if (lb := _table(tables, "lb")) is not None:
        frame, e, p = _lb(lb, animals)
        cont.append(frame.select("endpoint", "USUBJID", "value"))
        eps |= e
        problems += p
    if (om := _table(tables, "om")) is not None:
        frame, e, p = _om(om, bw, animals)
        cont.append(frame.select("endpoint", "USUBJID", "value"))
        eps |= e
        problems += p
    for domain, by_record in (("mi", True), ("ma", False), ("cl", False)):
        if (t := _table(tables, domain)) is not None:
            frame, e = _findings_domain(t, domain, animals, examined_by_record=by_record)
            inc.append(frame)
            eps |= e
    inc.append(
        animals.select(
            pl.lit("death").alias("endpoint"),
            "USUBJID",
            pl.col("unscheduled").alias("affected"),
            pl.lit(None, dtype=pl.Int64).alias("grade"),
        )
    )
    eps["death"] = Endpoint("death", "Unscheduled death", "incidence", "ds")
    return Values(eps, pl.concat(cont), pl.concat(inc), tuple(problems))


# ------------------------------------------------------------------ group summaries


@dataclass(frozen=True, slots=True)
class GroupStat:
    rank: int
    dose: float
    n: int
    mean: float | None = None
    sd: float | None = None
    pct: float | None = None  # mean vs the control mean, percent
    pct_without_extreme: float | None = None
    p: float | None = None  # Dunnett (continuous) or Fisher (incidence) against control
    affected: int = 0
    grades: tuple[tuple[int, int], ...] = ()  # (grade, animals), incidence only
    flagged: bool = False


@dataclass(frozen=True, slots=True)
class Summary:
    endpoint: Endpoint
    sex: str
    phase: str
    groups: tuple[GroupStat, ...]
    trend_z: float | None
    trend_p: float | None
    check: str | None  # the finding rule met, if any
    magnitude: float = 0.0  # continuous: the magnitude its groups were judged at (percent)

    @property
    def key(self) -> str:
        return f"{self.endpoint.endpoint}:{self.sex}:{self.phase}"


@dataclass(frozen=True, slots=True)
class Analysis:
    design: Design
    summaries: tuple[Summary, ...]
    problems: tuple[Problem, ...]


def _mean_sd(xs: list[float]) -> tuple[float, float]:
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) if len(xs) > 1 else 0.0
    return m, sd


def _pct(mean: float, control: float) -> float | None:
    return None if control == 0 else (mean - control) / abs(control) * 100


def _continuous(
    ep: Endpoint,
    sex: str,
    phase: str,
    by_rank: dict[int, list[float]],
    doses: list[float],
    cfg: ToxStudyMonitorConfig,
    problems: list[Problem],
    magnitude: float | None = None,
) -> Summary | None:
    control = by_rank.get(0, [])
    if len(control) < cfg.min_per_group:
        problems.append(
            Problem(
                "control_too_few",
                f"{ep.endpoint}:{sex}:{phase}",
                ep.endpoint.split(":")[0],
                len(control),
            )
        )
        return None
    c_mean, c_sd = _mean_sd(control)
    tested = [r for r in sorted(by_rank) if r > 0 and len(by_rank[r]) >= cfg.min_per_group]
    varies = len({v for r in [0, *tested] for v in by_rank[r]}) > 1
    ps = (
        dunnett(control, [by_rank[r] for r in tested], cfg.dunnett_seed)
        if tested and varies
        else [1.0] * len(tested)
    )
    p_of = dict(zip(tested, ps, strict=True))
    mag = (
        magnitude
        if magnitude is not None
        else cfg.magnitude_pct.get(ep.cls, cfg.magnitude_pct.get("other", 20.0))
    )
    stats: list[GroupStat] = [GroupStat(0, doses[0], len(control), c_mean, c_sd, 0.0)]
    for r in sorted(by_rank):
        if r == 0:
            continue
        xs = by_rank[r]
        m, sd = _mean_sd(xs)
        pct = _pct(m, c_mean)
        p = p_of.get(r)
        robust = None
        if len(xs) > 1:
            extreme = max(xs, key=lambda x: abs(x - c_mean))
            rest = list(xs)
            rest.remove(extreme)
            robust = _pct(sum(rest) / len(rest), c_mean)
        flagged = (
            p is not None
            and p < cfg.alpha_pairwise
            and pct is not None
            and robust is not None
            and abs(pct) >= mag
            and abs(robust) >= mag
        )
        stats.append(GroupStat(r, doses[r], len(xs), m, sd, pct, robust, p, flagged=flagged))
    trend = jonckheere([by_rank[r] for r in [0, *tested]]) if tested and varies else None
    tz, tp = (None, None) if trend is None else (trend.z, trend.p)
    return Summary(ep, sex, phase, tuple(stats), tz, tp, _continuous_check(stats, tz, tp, cfg), mag)


def _continuous_check(
    stats: list[GroupStat] | tuple[GroupStat, ...],
    trend_z: float | None,
    trend_p: float | None,
    cfg: ToxStudyMonitorConfig,
) -> str | None:
    flagged_groups = [g for g in stats if g.flagged]
    if not flagged_groups:
        return None
    same_way = trend_z is not None and all((g.pct or 0) * trend_z > 0 for g in flagged_groups)
    if trend_p is not None and trend_p < cfg.alpha_trend and same_way:
        return DOSE_RELATED_CHANGE
    if any(g.p is not None and g.p < cfg.alpha_pairwise_alone for g in flagged_groups):
        return PAIRWISE_CHANGE
    return None


def _red_cell_coherence(summaries: list[Summary], cfg: ToxStudyMonitorConfig) -> dict[str, Summary]:
    """1.1.0: a red cell group flagged at the red cell magnitude stays flagged only if
    another red cell parameter is flagged in the same direction at the same dose (same sex
    and phase); otherwise it must meet the hematology magnitude, with and without its most
    extreme animal. Returns the changed summaries by key."""
    hem = cfg.magnitude_pct.get("hematology", 10.0)
    out: dict[str, Summary] = {}
    for s in summaries:
        others = [o for o in summaries if o is not s]
        groups = []
        for g in s.groups:
            coherent = any(
                og.rank == g.rank and og.flagged and (og.pct or 0) * (g.pct or 0) > 0
                for o in others
                for og in o.groups
            )
            keep = g.flagged and (
                coherent
                or (
                    g.pct is not None
                    and g.pct_without_extreme is not None
                    and abs(g.pct) >= hem
                    and abs(g.pct_without_extreme) >= hem
                )
            )
            groups.append(replace(g, flagged=keep))
        if any(a.flagged != b.flagged for a, b in zip(groups, s.groups, strict=True)):
            out[s.key] = replace(
                s,
                groups=tuple(groups),
                check=_continuous_check(groups, s.trend_z, s.trend_p, cfg),
            )
    return out


def _incidence(
    ep: Endpoint,
    sex: str,
    phase: str,
    by_rank: dict[int, tuple[int, int, Counter[int]]],
    doses: list[float],
    cfg: ToxStudyMonitorConfig,
    problems: list[Problem],
) -> Summary | None:
    if 0 not in by_rank or by_rank[0][1] == 0:
        problems.append(Problem("control_too_few", f"{ep.endpoint}:{sex}:{phase}", ep.cls, 0))
        return None
    ranks = sorted(r for r in by_rank if by_rank[r][1] > 0)
    c_aff, c_n, _ = by_rank[0]
    stats = []
    for r in ranks:
        aff, n, grades = by_rank[r]
        p = None if r == 0 else fisher_greater(aff, n, c_aff, c_n)
        stats.append(
            GroupStat(
                r,
                doses[r],
                n,
                affected=aff,
                p=p,
                pct=aff / n * 100,
                grades=tuple(sorted(grades.items())),
            )
        )
    trend = cochran_armitage_exact(
        [by_rank[r][0] for r in ranks], [by_rank[r][1] for r in ranks], list(ranks)
    )

    def excess(g: GroupStat) -> bool:
        return (
            g.affected - c_aff >= cfg.incidence_min_excess_animals
            and (g.pct or 0) - c_aff / c_n * 100 >= cfg.incidence_min_excess_points
        )

    top = stats[-1]
    flagged = [g for g in stats[1:] if g.p is not None and excess(g)]
    check = None
    if len(stats) > 1 and trend.p < cfg.alpha_incidence_trend and excess(top):
        check = DOSE_RELATED_INCIDENCE
    elif any(g.p is not None and g.p < cfg.alpha_incidence_pairwise_alone for g in flagged):
        check = PAIRWISE_INCIDENCE
    marked = tuple(
        GroupStat(
            g.rank,
            g.dose,
            g.n,
            pct=g.pct,
            p=g.p,
            affected=g.affected,
            grades=g.grades,
            flagged=g.rank > 0
            and excess(g)
            and g.p is not None
            and (check == DOSE_RELATED_INCIDENCE or g.p < cfg.alpha_incidence_pairwise_alone),
        )
        for g in stats
    )
    return Summary(ep, sex, phase, marked, trend.z, trend.p, check)


def analyse(tables: Mapping[str, pl.DataFrame], cfg: ToxStudyMonitorConfig) -> Analysis:
    """Every endpoint, sex and phase of the study, with its tests and the rule it meets."""
    dm, tx = tables.get("dm"), tables.get("tx")
    if dm is None or tx is None:
        return Analysis(
            Design((), _EMPTY_CONT, "", ()), (), (Problem("missing_design", "dm/tx", "dm", 1),)
        )
    design = read_design(dm, tx, tables.get("ds"))
    values = endpoint_values(tables, design)
    problems = list(design.problems) + list(values.problems)
    animals = design.animals.select("USUBJID", "SEX", "phase", "rank")
    cont = values.continuous.join(animals, on="USUBJID")
    inc = values.incidence.join(animals, on="USUBJID")
    summaries: list[Summary] = []
    red_cells: list[Summary] = []
    for phase in (MAIN, RECOVERY):
        doses = design.doses(phase)
        if not doses:
            continue
        for sex in SEXES:
            c = cont.filter((pl.col("phase") == phase) & (pl.col("SEX") == sex))
            for endpoint in sorted(c["endpoint"].unique().to_list()):
                part = c.filter(pl.col("endpoint") == endpoint).sort(["rank", "USUBJID"])
                by_rank: dict[int, list[float]] = {}
                for r, v in part.select("rank", "value").iter_rows():
                    by_rank.setdefault(int(r), []).append(float(v))
                red = endpoint.removeprefix("lb:") in cfg.red_cell_tests
                s = _continuous(
                    values.endpoints[endpoint],
                    sex,
                    phase,
                    by_rank,
                    doses,
                    cfg,
                    problems,
                    cfg.red_cell_magnitude_pct if red else None,
                )
                if s is not None:
                    summaries.append(s)
                    if red:
                        red_cells.append(s)
            for key, changed in _red_cell_coherence(red_cells, cfg).items():
                summaries = [changed if x.key == key else x for x in summaries]
            red_cells = []
            i = inc.filter((pl.col("phase") == phase) & (pl.col("SEX") == sex))
            for endpoint in sorted(i["endpoint"].unique().to_list()):
                part = i.filter(pl.col("endpoint") == endpoint)
                if not part["affected"].any():
                    continue
                counts: dict[int, tuple[int, int, Counter[int]]] = {}
                for r, sub in part.group_by("rank", maintain_order=True):
                    grades = Counter(
                        int(g) for g in sub.filter(pl.col("affected"))["grade"].drop_nulls()
                    )
                    counts[int(r[0])] = (int(sub["affected"].sum()), sub.height, grades)
                s = _incidence(values.endpoints[endpoint], sex, phase, counts, doses, cfg, problems)
                if s is not None:
                    summaries.append(s)
    return Analysis(design, tuple(summaries), tuple(sorted(problems, key=lambda p: p.check)))


def by_key(analysis: Analysis) -> dict[str, Summary]:
    return {s.key: s for s in analysis.summaries}


Formatter = Callable[[float], str]
