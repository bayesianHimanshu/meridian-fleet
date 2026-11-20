"""The preclinical pages (phase 5 step 1): a SEND study's design, its endpoint tables with
the agent's flags, and its NOAEL decisions. Pure views over a study reader, the analysis
the agent itself uses (meridian.preclinical.analysis) and ledger events; who may see or
record anything is decided by the Repository through meridian.api.visibility."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import polars as pl

from meridian.api.models import (
    DoseGroupView,
    DoseSupport,
    EndpointGroupView,
    EndpointTableView,
    GradeCount,
    NoaelDecisionView,
    PreclinicalEndpoints,
    PreclinicalStudy,
)
from meridian.data.study_store import MissingTableError, StudyReader
from meridian.domain.events import Event, NoaelDecision
from meridian.domain.findings import Finding
from meridian.platform.agent_settings import ToxStudyMonitorConfig
from meridian.preclinical.analysis import (
    DOSE_RELATED_CHANGE,
    DOSE_RELATED_INCIDENCE,
    MAIN,
    Analysis,
    analyse,
    read_design,
)
from meridian.preclinical.rules import raise_findings

DOMAINS = ("bw", "cl", "dd", "dm", "ds", "lb", "ma", "mi", "om", "ts", "tx")


def tables(reader: StudyReader) -> dict[str, pl.DataFrame]:
    out = {}
    for name in DOMAINS:
        try:
            out[name] = reader.table(name)
        except MissingTableError:
            continue
    return out


def _ts(ts: pl.DataFrame | None, code: str) -> str:
    if ts is None:
        return ""
    rows = ts.filter(pl.col("TSPARMCD") == code)["TSVAL"].to_list()
    return str(rows[0]) if rows else ""


def study_view(
    reader: StudyReader, *, study_id: str, asset_id: str | None, cut_id: str, seq: int
) -> PreclinicalStudy:
    t = tables(reader)
    design = read_design(t["dm"], t["tx"], t.get("ds"))
    counts = {
        (s, setcd): n
        for s, setcd, n in design.animals.group_by(["SEX", "SETCD"], maintain_order=True)
        .len()
        .rows()
    }
    groups = [
        DoseGroupView(
            setcd=g.setcd,
            phase=g.phase,  # type: ignore[arg-type]
            dose=g.dose,
            control=g.control,
            males=counts.get(("M", g.setcd), 0),
            females=counts.get(("F", g.setcd), 0),
        )
        for g in sorted(design.groups, key=lambda g: (g.phase != MAIN, g.rank))
    ]
    ts = t.get("ts")
    return PreclinicalStudy(
        study_id=study_id,
        asset_id=asset_id,
        cut_id=cut_id,
        loaded_seq=seq,
        title=_ts(ts, "STITLE"),
        species=_ts(ts, "SPECIES"),
        strain=_ts(ts, "STRAIN"),
        route=_ts(ts, "ROUTE"),
        duration=_ts(ts, "DOSDUR"),
        dose_unit=design.dose_unit,
        groups=groups,
    )


def main_doses(reader: StudyReader) -> tuple[list[float], str]:
    t = tables(reader)
    design = read_design(t["dm"], t["tx"], t.get("ds"))
    return design.doses(MAIN), design.dose_unit


def design_animals(reader: StudyReader) -> pl.DataFrame:
    """Every analysed animal: USUBJID, SEX, phase and dose (meridian.preclinical.analysis)."""
    t = tables(reader)
    return read_design(t["dm"], t["tx"], t.get("ds")).animals


@dataclass(frozen=True, slots=True)
class FindingProfile:
    """Which sex a preclinical finding is about and the treated doses where it flags a
    group, per phase, read from its evidence. A finding about one animal (an unscheduled
    death) names it by USUBJID; its sex, phase and dose come from the study design."""

    sexes: frozenset[str]
    main_doses: tuple[float, ...]
    recovery_doses: tuple[float, ...]

    @property
    def sex(self) -> str | None:
        return next(iter(self.sexes)) if len(self.sexes) == 1 else None


def _fields(selector: str) -> dict[str, str]:
    out = {}
    for part in selector.split(";"):
        key, eq, value = part.strip().partition("=")
        if eq:
            out[key.strip()] = value.strip()
    return out


def _dose(text: str) -> float | None:
    try:
        return float(text.split()[0])
    except IndexError, ValueError:
        return None


def finding_profile(finding: Finding, animals: pl.DataFrame) -> FindingProfile:
    sexes: set[str] = set()
    doses: dict[str, set[float]] = {"main": set(), "recovery": set()}
    by_animal = {
        str(r["USUBJID"]): r
        for r in animals.select("USUBJID", "SEX", "phase", "dose").iter_rows(named=True)
    }
    for e in finding.evidence:
        f = _fields(e.selector)
        if "SEX" in f:
            sexes.add(f["SEX"])
            dose = _dose(f.get("DOSE", ""))
            if e.role == "primary" and dose is not None and dose > 0 and f.get("PHASE") in doses:
                doses[f["PHASE"]].add(dose)
        elif "USUBJID" in f and f["USUBJID"] in by_animal:
            a = by_animal[f["USUBJID"]]
            sexes.add(str(a["SEX"]))
            if e.role == "primary" and float(a["dose"]) > 0 and a["phase"] in doses:
                doses[str(a["phase"])].add(float(a["dose"]))
        elif "ENDPOINT" in f:  # a data-quality finding: <endpoint>:<sex>:<phase>, if any
            parts = f["ENDPOINT"].split(":")
            if len(parts) >= 3 and parts[-2] in ("M", "F"):
                sexes.add(parts[-2])
    return FindingProfile(
        frozenset(sexes & {"M", "F"}),
        tuple(sorted(doses["main"])),
        tuple(sorted(doses["recovery"])),
    )


def endpoints_view(
    reader: StudyReader,
    cfg: ToxStudyMonitorConfig,
    *,
    study_id: str,
    cut_id: str,
    finding_by_check: Mapping[str, str],
) -> PreclinicalEndpoints:
    """Every endpoint table, with the finding the agent raised on it when the caller can
    see that finding (finding_by_check holds only those)."""
    t = tables(reader)
    analysis: Analysis = analyse(t, cfg)
    raised = raise_findings(analysis, t, cfg)  # 1.1.0: the finding that carries each table
    views = []
    support: dict[tuple[str, float], tuple[list[str], list[str]]] = {}
    for s in analysis.summaries:
        carrier = raised.carried_by.get(s.key)
        fid = finding_by_check.get(carrier) if carrier else None
        views.append(
            EndpointTableView(
                endpoint=s.endpoint.endpoint,
                label=s.endpoint.label,
                kind=s.endpoint.kind,  # type: ignore[arg-type]
                unit=s.endpoint.unit,
                sex=s.sex,  # type: ignore[arg-type]
                phase=s.phase,  # type: ignore[arg-type]
                groups=[
                    EndpointGroupView(
                        rank=g.rank,
                        dose=g.dose,
                        n=g.n,
                        mean=g.mean,
                        sd=g.sd,
                        percent_vs_control=None if g.rank == 0 else g.pct,
                        p=g.p,
                        affected=g.affected if s.endpoint.kind == "incidence" else None,
                        grades=[GradeCount(grade=a, animals=b) for a, b in g.grades],
                        flagged=g.flagged,
                    )
                    for g in s.groups
                ],
                trend_p=s.trend_p,
                check=carrier.split(":", 1)[0] if carrier else None,
                finding_id=fid,
            )
        )
        if fid is None:
            continue
        related = (
            carrier is not None
            and carrier.split(":", 1)[0] in (DOSE_RELATED_CHANGE, DOSE_RELATED_INCIDENCE)
            and s.phase == MAIN
        )
        for g in s.groups:
            if g.rank > 0 and (g.flagged or (s.endpoint.neoplastic and g.affected > 0)):
                slot = support.setdefault((s.phase, g.dose), ([], []))
                slot[0 if related else 1].append(fid)
    by_dose = [
        DoseSupport(
            phase=phase,  # type: ignore[arg-type]
            dose=dose,
            dose_related=sorted(set(a)),
            without_trend=sorted(set(b)),
        )
        for (phase, dose), (a, b) in sorted(
            support.items(), key=lambda kv: (kv[0][0] != MAIN, kv[0][1])
        )
    ]
    return PreclinicalEndpoints(
        study_id=study_id,
        cut_id=cut_id,
        dose_unit=analysis.design.dose_unit,
        tables=views,
        by_dose=by_dose,
    )


def noael_views(events: Sequence[Event]) -> list[NoaelDecisionView]:
    """Every NOAEL decision in seq order, each marked current unless superseded."""
    decisions = [e for e in events if isinstance(e.payload, NoaelDecision)]
    superseded_by: dict[str, str] = {}
    for e in decisions:
        p = e.payload
        assert isinstance(p, NoaelDecision)
        if p.supersedes is not None:
            superseded_by[p.supersedes] = p.decision_id
    out = []
    for e in decisions:
        p = e.payload
        assert isinstance(p, NoaelDecision)
        out.append(
            NoaelDecisionView(
                seq=e.seq,
                recorded_at=e.recorded_at,
                decision_id=p.decision_id,
                study_id=p.study_id,
                sex=p.sex,
                established=p.established,
                noael_dose=p.noael_dose,
                loael_dose=p.loael_dose,
                dose_unit=p.dose_unit,
                finding_ids=list(p.finding_ids),
                rationale=p.rationale,
                decided_by=p.decided_by,
                supersedes=p.supersedes,
                superseded_by=superseded_by.get(p.decision_id),
                current=p.decision_id not in superseded_by,
            )
        )
    return out
