"""IND readiness rules (phase 5 step 2), as pre-registered in docs/ind-readiness-eval.md:
an asset's nonclinical study inventory against the first-in-human requirements of
config/ind_requirements.toml. Pure: preclinical.ind_readiness words the gaps as findings,
and the API computes the same checklist and percentage from the same functions.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import polars as pl

from meridian.platform.agent_settings import IndReadinessConfig, IndRequirement
from meridian.preclinical.inventory import COLUMNS, InventoryStudy

MISSING = "required_study_missing"
DURATION_SHORT = "duration_short"
REPORT_NOT_READY = "report_not_ready"
GLP_MISSING = "glp_status_missing"
INVENTORY_TABLE = "studies"


class InventoryError(ValueError):
    pass


def _date(text: str) -> date | None:
    return date.fromisoformat(text) if text else None


def _weeks(text: str) -> float | None:
    return float(text) if text else None


def studies_from(frame: pl.DataFrame) -> list[InventoryStudy]:
    """The inventory table (every column a string, as in the committed CSV)."""
    missing = [c for c in COLUMNS if c not in frame.columns]
    if missing:
        raise InventoryError(f"the inventory lacks columns {missing}")
    out = []
    for r in frame.select(COLUMNS).iter_rows(named=True):
        v = {k: "" if r[k] is None else str(r[k]).strip() for k in COLUMNS}
        out.append(
            InventoryStudy(
                study_id=v["study_id"],
                title=v["title"],
                study_type=v["study_type"],
                species=v["species"].upper(),
                route=v["route"],
                duration_weeks=_weeks(v["duration_weeks"]),
                glp=v["glp"].upper(),
                report_status=v["report_status"],
                start_date=_date(v["start_date"]),
                report_final_date=_date(v["report_final_date"]),
                qa_audit_date=_date(v["qa_audit_date"]),
                part_of=v["part_of"],
            )
        )
    return out


def required_weeks(req: IndRequirement, cfg: IndReadinessConfig) -> float | None:
    """ICH M3(R2) Table 1 for the planned clinical duration (footnote a not used)."""
    if req.duration != "table1":
        return None
    days = cfg.planned_clinical_duration_days
    if days <= 14:
        return 2.0
    if days <= 182:
        return days / 7
    return 26.0 if req.species_class == "rodent" else 39.0


def applicable(req: IndRequirement, cfg: IndReadinessConfig) -> bool:
    return req.applies == "always" or cfg.multiple_dose


def candidates(
    req: IndRequirement, studies: Sequence[InventoryStudy], cfg: IndReadinessConfig
) -> list[InventoryStudy]:
    species = (
        None
        if req.species_class == "any"
        else set(cfg.requirements.classes.get(req.species_class, ()))
    )
    return [
        s
        for s in studies
        if s.study_type in req.study_types and (species is None or s.species in species)
    ]


def report_ready(s: InventoryStudy, cfg: IndReadinessConfig) -> bool:
    """QA audited by the planned submission date: audited already, or an audit dated (done
    or planned) on or before it."""
    if s.report_status == "qa_audited":
        return True
    return s.qa_audit_date is not None and s.qa_audit_date <= cfg.planned_submission_date


def problems_of(req: IndRequirement, s: InventoryStudy, cfg: IndReadinessConfig) -> tuple[str, ...]:
    out = []
    need = required_weeks(req, cfg)
    if need is not None and (s.duration_weeks is None or s.duration_weeks < need):
        out.append(DURATION_SHORT)
    if not report_ready(s, cfg):
        out.append(REPORT_NOT_READY)
    if not s.glp:
        out.append(GLP_MISSING)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class RequirementStatus:
    requirement: IndRequirement
    applicable: bool
    required_weeks: float | None
    candidates: tuple[InventoryStudy, ...]
    study: InventoryStudy | None  # the chosen candidate
    problems: tuple[str, ...]

    @property
    def met(self) -> bool:
        return self.applicable and self.study is not None and not self.problems

    @property
    def status(self) -> str:
        if not self.applicable:
            return "not_applicable"
        return "met" if self.met else "not_met"

    def checks(self) -> list[str]:
        """The finding check of each gap: one per problem of the chosen study, or missing."""
        if not self.applicable:
            return []
        rid = self.requirement.id
        if self.study is None:
            return [f"{MISSING}:{rid}"]
        return [f"{p}:{rid}:{self.study.study_id}" for p in self.problems]


def assess(studies: Sequence[InventoryStudy], cfg: IndReadinessConfig) -> list[RequirementStatus]:
    out = []
    for req in cfg.requirements.requirements:
        cands = candidates(req, studies, cfg)
        chosen = (
            min(
                cands,
                key=lambda s: (
                    len(problems_of(req, s, cfg)),
                    -(s.duration_weeks or 0.0),
                    s.study_id,
                ),
            )
            if cands
            else None
        )
        out.append(
            RequirementStatus(
                requirement=req,
                applicable=applicable(req, cfg),
                required_weeks=required_weeks(req, cfg),
                candidates=tuple(cands),
                study=chosen,
                problems=problems_of(req, chosen, cfg) if chosen is not None else (),
            )
        )
    return out


def percent_met(statuses: Sequence[RequirementStatus]) -> float:
    """Percent of applicable requirements met (100 when none applies)."""
    mine = [s for s in statuses if s.applicable]
    return 100.0 if not mine else 100.0 * sum(s.met for s in mine) / len(mine)
