"""preclinical.ind_readiness 1.0.0: MRD-101's nonclinical study inventory against what a
first-in-human trial needs, as pre-registered in docs/ind-readiness-eval.md (rules in
meridian.preclinical.readiness, requirements in config/ind_requirements.toml).

It wakes on an InventoryLoaded event and reads only the inventory, staged as
/stage/inventory. One finding per gap: a required study missing, a duration shorter than
ICH M3(R2) Table 1 asks for the planned trial, a report not QA audited by the planned
submission date, a GLP status not recorded. Each quotes the guidance it rests on. The
percent of requirements met is a summary, not a finding (the API computes it). It never
says the programme is ready for submission.
"""

from collections.abc import Sequence

from meridian.agents.base import AgentContext, FindingDraft, Zone
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event, InventoryLoaded
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import IndReadinessConfig
from meridian.preclinical.inventory import InventoryStudy
from meridian.preclinical.readiness import (
    DURATION_SHORT,
    GLP_MISSING,
    INVENTORY_TABLE,
    MISSING,
    REPORT_NOT_READY,
    RequirementStatus,
    assess,
    studies_from,
)

AGENT_ID = "preclinical.ind_readiness"
VERSION = "1.0.0"
VISIBLE_TO_MAX = frozenset({Role.TOXICOLOGIST, Role.PORTFOLIO_LEAD, Role.QA_AUDITOR})
STATUS_TEXT = {
    "planned": "planned",
    "ongoing": "ongoing",
    "draft": "a draft",
    "final": "final, not QA audited",
    "qa_audited": "QA audited",
}


def _d(value: object) -> str:
    return "not recorded" if value is None else str(value)


def _citation(st: RequirementStatus) -> Evidence:
    r = st.requirement
    return Evidence(
        source=f"config:ind_requirements:{r.id}",
        selector=f"REQUIREMENT={r.id}",
        statistic="ind:requirement",
        value=0.0,
        role="supporting",
        direction="none",
        display=f'{r.source} {r.section}: "{r.quote}"' + (f" Note: {r.note}" if r.note else ""),
    )


def _study_evidence(s: InventoryStudy, statistic: str, value: float, display: str) -> Evidence:
    return Evidence(
        source="inventory:studies",
        selector=f"STUDY={s.study_id}",
        statistic=statistic,
        value=value,
        role="primary",
        direction="none",
        display=display,
    )


def _gap(
    check: str, st: RequirementStatus, cfg: IndReadinessConfig
) -> tuple[str, str, list[Evidence], Severity]:
    r = st.requirement
    kind = check.split(":", 1)[0]
    cite = f"{r.source} {r.section}"
    s = st.study
    if kind == MISSING or s is None:
        title = f'IND readiness: no study for "{r.title}"'
        types = " or ".join(t.replace("_", " ") for t in r.study_types)
        cls = r.species_class.replace("_", " ")
        where = "" if r.species_class == "any" else f" in a {cls} species"
        summary = (
            f"The inventory has no {types} study{where}. {cite} expects it before the first "
            "human trial."
        )
        ev = Evidence(
            source="inventory:studies",
            selector=f"REQUIREMENT={r.id}",
            statistic="ind:candidates",
            value=0.0,
            role="primary",
            direction="none",
            display="0 studies of the required type and species in the inventory",
        )
        return title, summary, [ev], Severity.HIGH
    if kind == DURATION_SHORT:
        need = st.required_weeks or 0.0
        have = s.duration_weeks
        title = f"IND readiness: {s.study_id} is shorter than the planned trial needs"
        summary = (
            f"{s.title} ({s.study_id}) lasts {_d(have) if have is None else f'{have:g}'} weeks; "
            f"for a trial of {cfg.planned_clinical_duration_days} days of dosing ICH M3(R2) "
            f"Table 1 asks for at least {need:g} weeks ({r.title.lower()})."
        )
        ev = _study_evidence(
            s,
            "ind:duration_weeks",
            have or 0.0,
            f"{s.study_id}: {_d(have) if have is None else f'{have:g}'} weeks, at least "
            f"{need:g} required",
        )
        return title, summary, [ev], Severity.MEDIUM
    if kind == REPORT_NOT_READY:
        audit = f"planned for {s.qa_audit_date}" if s.qa_audit_date else "not planned"
        title = f"IND readiness: the {s.study_id} report is not QA audited by the submission date"
        summary = (
            f"{s.title} ({s.study_id}) is {STATUS_TEXT.get(s.report_status, s.report_status)}; "
            f"its QA audit is {audit}, "
            f"after the planned IND submission on {cfg.planned_submission_date} "
            f"({r.title.lower()}). FDA's 1995 Phase 1 IND guidance (III.G.2) allows a summary "
            "on unaudited drafts with the final reports within 120 days of the human study's "
            "start; this measures the programme's own plan."
        )
        ev = _study_evidence(
            s,
            "ind:report_status",
            0.0,
            f"{s.study_id}: report {STATUS_TEXT.get(s.report_status, s.report_status)}, final "
            f"{_d(s.report_final_date)}, QA audit {_d(s.qa_audit_date)}; submission "
            f"{cfg.planned_submission_date}",
        )
        return title, summary, [ev], Severity.MEDIUM
    title = f"IND readiness: {s.study_id} has no GLP status recorded"
    summary = (
        f'{s.title} ({s.study_id}) is the study that meets "{r.title}", and the inventory '
        "does not say whether it was conducted under GLP. An IND declares, for each study "
        "subject to GLP, that it complied or why not (21 CFR 312.23(a)(8)(iii), as FDA's "
        "1995 Phase 1 IND guidance, III.G.2.e, describes)."
    )
    ev = _study_evidence(s, "ind:glp", 0.0, f"{s.study_id}: GLP status not recorded")
    return title, summary, [ev], Severity.MEDIUM


class IndReadiness:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.PRECLINICAL
    subscribes_to = frozenset({EventType.INVENTORY_LOADED})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = IndReadinessConfig

    def run(self, trigger: Event, ctx: AgentContext[IndReadinessConfig]) -> Sequence[FindingDraft]:
        if not isinstance(trigger.payload, InventoryLoaded):
            raise ValueError(f"{AGENT_ID} wakes on an inventory load, not {trigger.payload.type}")
        if trigger.payload.cut_id != ctx.study.cut_id:
            raise ValueError(
                f"trigger is for inventory {trigger.payload.cut_id}, the reader for "
                f"{ctx.study.cut_id}"
            )
        studies = studies_from(ctx.study.table(INVENTORY_TABLE))
        drafts = self.drafts(studies, ctx.config, ctx.study.study_id, ctx.study.cut_id)
        ctx.log.info("ind.assessed", studies=len(studies), gaps=len(drafts))
        return drafts

    def drafts(
        self,
        studies: Sequence[InventoryStudy],
        cfg: IndReadinessConfig,
        asset_id: str,
        cut_id: str,
    ) -> list[FindingDraft]:
        out = []
        for st in assess(studies, cfg):
            for check in st.checks():
                title, summary, evidence, severity = _gap(check, st, cfg)
                out.append(
                    FindingDraft(
                        dedup_key=make_dedup_key(
                            agent_id=AGENT_ID,
                            agent_version=VERSION,
                            kind=FindingKind.IND_READINESS,
                            study_id=asset_id,
                            site_id=None,
                            cut_id=cut_id,
                            check=check,
                        ),
                        kind=FindingKind.IND_READINESS,
                        site_id=None,
                        severity=severity,
                        title=title,
                        summary=summary
                        + " It is a gap in the plan, not a judgement that the IND can or "
                        "cannot be submitted.",
                        evidence=(*evidence, _citation(st)),
                        score=1.0 if severity is Severity.HIGH else 0.5,
                        visible_to=cfg.visible_to,
                    )
                )
        return out


__all__ = ["AGENT_ID", "GLP_MISSING", "VERSION", "IndReadiness"]
