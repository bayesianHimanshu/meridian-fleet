"""preclinical.tox_study_monitor 1.1.0: what changed with dose in a repeat-dose study.

Rules: docs/preclinical-eval.md (1.0.0, and the 1.1.0 changes, each pre-registered before
its code was written). The analysis is meridian.preclinical.analysis and the 1.1.0 rules
meridian.preclinical.rules; this module words each item they raise as a finding: one per
endpoint and sex with the recovery status as evidence, recovery-only changes, relative organ
weights that may be secondary to lower body weight, neoplasms, unscheduled deaths with their
cause, and each kind of unusable record as a data-quality finding. Against the concurrent
control only. The agent says "dose-related" or "no trend with dose"; it never judges
adversity and never proposes a NOAEL or a LOAEL, which the toxicologist records
(NoaelDecision). One bad record never fails the run.
"""

from collections.abc import Mapping, Sequence
from typing import Literal

import polars as pl

from meridian.agents.base import AgentContext, FindingDraft, Zone
from meridian.data.study_store import MissingTableError, StudyReader
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event, SendStudyLoaded
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import ToxStudyMonitorConfig
from meridian.preclinical.analysis import (
    DATA_QUALITY,
    DOSE_RELATED_CHANGE,
    DOSE_RELATED_INCIDENCE,
    MAIN,
    PAIRWISE_CHANGE,
    Analysis,
    GroupStat,
    Problem,
    Summary,
    analyse,
)
from meridian.preclinical.rules import (
    DeathItem,
    EndpointItem,
    NeoplasmItem,
    RecoveryStatus,
    SecondaryItem,
    raise_findings,
)

AGENT_ID = "preclinical.tox_study_monitor"
VERSION = "1.1.0"
VISIBLE_TO_MAX = frozenset({Role.TOXICOLOGIST, Role.PORTFOLIO_LEAD, Role.QA_AUDITOR})
DOMAINS = ("bg", "bw", "cl", "dd", "dm", "ds", "ex", "fw", "lb", "ma", "mi", "om", "ts", "tx")
SEX = {"M": "males", "F": "females"}
PHASE = {MAIN: "main study", "recovery": "recovery groups"}
PROBLEM_TEXT = {
    "unit_mismatch": "values in a unit other than the one most values of the test use",
    "missing_unit": "numeric values with no unit",
    "duplicate": "more than one value for one animal on one day",
    "control_too_few": "too few control values to compare with",
    "unknown_set": "animals in a set that TX does not define",
    "unknown_sex": "animals with a sex other than M or F",
    "bad_dose": "a set with no numeric dose",
    "control_ambiguous": "no single control group",
    "missing_design": "no DM or TX domain",
}


def _p(p: float | None) -> str:
    if p is None:
        return "not tested"
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def _num(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.4g}"


def _dose(g: GroupStat, unit: str) -> str:
    return "control" if g.rank == 0 else f"{g.dose:g} {unit}"


def _tables(study: StudyReader) -> dict[str, pl.DataFrame]:
    out = {}
    for name in DOMAINS:
        try:
            out[name] = study.table(name)
        except MissingTableError:
            continue
    return out


def _group_evidence(
    s: Summary, g: GroupStat, unit: str, *, primary: bool | None = None
) -> Evidence:
    ep = s.endpoint
    where = f"SEX={s.sex}; PHASE={s.phase}; DOSE={g.dose:g} {unit}"
    role: Literal["primary", "supporting"] = (
        "primary" if (g.flagged if primary is None else primary) else "supporting"
    )
    at = "" if s.phase == MAIN else " (recovery)"
    if ep.kind == "continuous":
        tail = (
            ""
            if g.rank == 0
            else f", {g.pct:+.0f}%, Dunnett {_p(g.p)}"
            if g.pct is not None
            else ""
        )
        direction: Literal["low", "high", "none"] = (
            "none" if g.rank == 0 or g.pct is None else "high" if g.pct > 0 else "low"
        )
        return Evidence(
            source=f"send:{ep.endpoint.split(':')[0]}",
            selector=where,
            statistic=f"group_mean:{ep.endpoint}",
            value=g.mean if g.mean is not None else 0.0,
            expected=s.groups[0].mean,
            role=role,
            direction=direction,
            display=f"{_dose(g, unit)}{at}: {_num(g.mean)} {ep.unit} (n = {g.n}{tail})".replace(
                " )", ")"
            ),
        )
    grades = ", ".join(f"grade {grade}: {n}" for grade, n in g.grades)
    sev = f"; {grades}" if grades else ""
    tail = "" if g.rank == 0 else f", Fisher {_p(g.p)}"
    return Evidence(
        source=f"send:{ep.cls}",
        selector=where,
        statistic=f"incidence:{ep.endpoint}",
        value=g.affected / g.n if g.n else 0.0,
        expected=s.groups[0].affected / s.groups[0].n if s.groups[0].n else None,
        role=role,
        direction="none" if g.rank == 0 else "high",
        display=f"{_dose(g, unit)}{at}: {g.affected} of {g.n}{sev}{tail}",
    )


def _trend_evidence(s: Summary, cfg: ToxStudyMonitorConfig) -> Evidence:
    continuous = s.endpoint.kind == "continuous"
    name = "Jonckheere-Terpstra" if continuous else "Cochran-Armitage, exact"
    alpha = cfg.alpha_trend if continuous else cfg.alpha_incidence_trend
    return Evidence(
        source=f"send:{s.endpoint.endpoint.split(':')[0]}",
        selector=f"SEX={s.sex}; PHASE={s.phase}",
        statistic=f"trend_p:{s.endpoint.endpoint}",
        value=s.trend_p if s.trend_p is not None else 1.0,
        threshold=alpha,
        role="primary"
        if s.check in (DOSE_RELATED_CHANGE, DOSE_RELATED_INCIDENCE) and s.phase == MAIN
        else "supporting",
        direction="none",
        display=f"trend across doses{'' if s.phase == MAIN else ' (recovery)'} "
        f"{_p(s.trend_p)} ({name})",
    )


def _text(s: Summary, unit: str) -> tuple[str, str]:
    ep = s.endpoint
    who = f"{SEX[s.sex]}, {PHASE[s.phase]}"
    flagged = [g for g in s.groups if g.flagged]
    at = " and ".join(f"{g.dose:g}" for g in flagged) + f" {unit}"
    if ep.kind == "continuous":
        way = "higher" if (flagged[0].pct or 0) > 0 else "lower"
        changes = "; ".join(
            f"{g.dose:g} {unit} {g.pct:+.0f}%" for g in flagged if g.pct is not None
        )
        control = s.groups[0]
        against = f"({changes} against the control mean of {_num(control.mean)} {ep.unit})"
        if s.check == DOSE_RELATED_CHANGE:
            title = f"{ep.label} {way} with dose in {who}"
            lead = f"Dose-related: mean {ep.label} is {way} at {at} {against}"
        else:
            title = f"{ep.label} {way} at {at} in {who}, no trend with dose"
            lead = (
                f"Mean {ep.label} is {way} at {at} {against} with no significant trend across doses"
            )
        rule = (
            "Each flagged group differs from control by Dunnett's test and by at least the "
            "pre-registered magnitude, also without its most extreme animal"
        )
    else:
        control = s.groups[0]
        base = f"control {control.affected} of {control.n}"
        if s.check == DOSE_RELATED_INCIDENCE:
            title = f"{ep.label} more frequent with dose in {who}"
            lead = f"Dose-related: incidence rises with dose ({base})"
        else:
            title = f"{ep.label} more frequent at {at} in {who}, no trend with dose"
            lead = f"Incidence is higher at {at} ({base}) with no significant trend"
        rule = "Incidence against the concurrent control; severity grades are in the evidence"
    summary = f"{lead}. Trend {_p(s.trend_p)}. {rule}."
    return title, summary


DECIDES = " The toxicologist decides what this means for the NOAEL."


def _change(value: float | None, kind: str) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.0f}%" if kind == "continuous" else f"{value:+.0f} points"


def _recovery_evidence(main: Summary, r: RecoveryStatus, unit: str) -> list[Evidence]:
    kind = main.endpoint.kind
    status = Evidence(
        source=f"send:{main.endpoint.endpoint.split(':')[0]}",
        selector=f"SEX={main.sex}; PHASE=recovery; DOSE={r.dose:g} {unit}",
        statistic=f"recovery_status:{main.endpoint.endpoint}",
        value=r.recovery_change if r.recovery_change is not None else 0.0,
        expected=r.main_change,
        role="supporting",
        direction="none",
        display=f"recovery at {r.dose:g} {unit}: {r.status} "
        f"({_change(r.main_change, kind)} at the end of dosing, "
        f"{_change(r.recovery_change, kind)} after recovery)",
    )
    rows = (
        [_group_evidence(r.summary, g, unit, primary=False) for g in r.summary.groups]
        if r.summary is not None
        else []
    )
    return [status, *rows]


def _endpoint_draft_text(item: EndpointItem, unit: str) -> tuple[str, str]:
    s = item.summary
    if item.recovery is None:
        title, summary = _text(s, unit)
        if s.phase != MAIN:
            title = f"Only at recovery: {title}"
            summary = (
                f"{summary} The main study shows no such change in {SEX[s.sex]}: it appears "
                "only after the recovery period."
            )
        return title, summary + DECIDES
    title, summary = _text(s, unit)
    r = item.recovery
    tail = f" At recovery ({r.dose:g} {unit}): {r.status}" + (
        f", {_change(r.main_change, s.endpoint.kind)} at the end of dosing and "
        f"{_change(r.recovery_change, s.endpoint.kind)} after recovery."
        if r.status != "not assessed"
        else "."
    )
    return title, summary + tail + DECIDES


def _secondary_text(item: SecondaryItem, unit: str) -> tuple[str, str, list[Evidence]]:
    who = f"{SEX[item.sex]}, {PHASE[item.phase]}"
    bw = item.body_weight
    parts = []
    evidence = [
        Evidence(
            source="send:bw",
            selector=f"SEX={item.sex}; PHASE={item.phase}; DOSE={item.dose:g} {unit}",
            statistic="group_mean:bw:BW",
            value=bw.mean if bw.mean is not None else 0.0,
            role="primary",
            direction="low",
            display=f"body weight at {item.dose:g} {unit}: {bw.pct:+.0f}% against control"
            if bw.pct is not None
            else f"body weight at {item.dose:g} {unit}",
        )
    ]
    for rel, absolute in item.organs:
        g = next(x for x in rel.groups if x.rank == item.rank)
        a = (
            next((x for x in absolute.groups if x.rank == item.rank), None)
            if absolute is not None
            else None
        )
        organ = rel.endpoint.label.removesuffix(" weight relative to body weight")
        absolute_text = (
            f"absolute {a.pct:+.0f}%, not flagged"
            if a is not None and a.pct is not None
            else "absolute not analysed"
        )
        parts.append(f"{organ} {g.pct:+.0f}% ({absolute_text})")
        evidence.append(_group_evidence(rel, g, unit, primary=True))
        if a is not None and absolute is not None:
            evidence.append(_group_evidence(absolute, a, unit, primary=False))
    title = (
        f"Relative organ weights changed at {item.dose:g} {unit} in {who}: may be secondary "
        "to lower body weight"
    )
    summary = (
        f"Body weight at {item.dose:g} {unit} is "
        f"{bw.pct:+.0f}% against control. Organ weights relative to body weight are flagged "
        f"at that dose with no matching change in absolute weight: {'; '.join(parts)}. "
        "These may be secondary to lower body weight rather than an effect on the organs." + DECIDES
    )
    return title, summary, evidence


def _neoplasm_text(item: NeoplasmItem, unit: str) -> tuple[str, str, list[Evidence]]:
    first = item.summaries[0]
    label = first.endpoint.label
    phases = []
    evidence: list[Evidence] = []
    for s in item.summaries:
        counts = ", ".join(
            f"{'control' if g.rank == 0 else f'{g.dose:g} {unit}'} {g.affected} of {g.n}"
            for g in s.groups
        )
        phases.append(f"{PHASE[s.phase]}: {counts}")
        evidence += [
            _group_evidence(s, g, unit, primary=g.rank > 0 and g.affected > 0) for g in s.groups
        ]
    main = next((s for s in item.summaries if s.phase == MAIN), None)
    trend = (
        f" Trend in the main study {_p(main.trend_p)} (Cochran-Armitage, exact)."
        if main is not None
        else ""
    )
    if main is not None:
        evidence.append(
            Evidence(
                source="send:mi",
                selector=f"SEX={main.sex}; PHASE={main.phase}",
                statistic=f"trend_p:{main.endpoint.endpoint}",
                value=main.trend_p if main.trend_p is not None else 1.0,
                role="supporting",
                direction="none",
                display=f"trend across doses {_p(main.trend_p)} (Cochran-Armitage, exact)",
            )
        )
    title = f"Neoplasm in treated {SEX[item.sex]}: {label}"
    summary = (
        f"{label} is a neoplasm, found in at least one animal of a treated group; any "
        f"neoplasm in a treated group is raised, whatever the statistics. "
        f"{'; '.join(phases)}.{trend}" + DECIDES
    )
    return title, summary, evidence


def _death_text(item: DeathItem, unit: str) -> tuple[str, str, list[Evidence]]:
    who = f"{'male' if item.sex == 'M' else 'female'}, {item.dose:g} {unit}, {PHASE[item.phase]}"
    day = f"day {item.day:g}" if item.day is not None else "an unrecorded day"
    disposition = item.disposition.lower() or "an unscheduled death"
    if item.cause_source == "MI":
        cause = "Cause in MI (marked as related to death): " + "; ".join(item.causes) + "."
    elif item.cause_source == "DD":
        cause = "Cause in DD (death diagnosis): " + "; ".join(item.causes) + "."
    else:
        cause = "No cause recorded in MI or DD."
    title = f"Unscheduled death in a treated group: {who}, {day}"
    summary = (
        f"Animal {item.usubjid} ({who}) was a {disposition} on {day}. {cause} Any unscheduled "
        "death in a treated group is raised, whatever the statistics." + DECIDES
    )
    evidence = [
        Evidence(
            source="send:ds",
            selector=f"USUBJID={item.usubjid}",
            statistic="unscheduled_death:day",
            value=item.day if item.day is not None else 0.0,
            role="primary",
            direction="none",
            display=f"{item.usubjid}: {disposition}, {day}",
        )
    ]
    for c in item.causes:
        evidence.append(
            Evidence(
                source="send:mi" if item.cause_source == "MI" else "send:dd",
                selector=f"USUBJID={item.usubjid}",
                statistic="unscheduled_death:cause",
                value=0.0,
                role="primary",
                direction="none",
                display=f"cause: {c}",
            )
        )
    return title, summary, evidence


def _problem_text(p: Problem) -> tuple[str, str, list[Evidence]]:
    what = PROBLEM_TEXT.get(p.kind, p.kind.replace("_", " "))
    where = p.detail.replace(":", " ")
    title = f"Data quality: {where}: {what}"
    effect = (
        "that endpoint is not compared in that sex and phase"
        if p.kind == "control_too_few"
        else "those records are left out; every other record is still analysed"
    )
    summary = f"{p.count} record(s) of {p.domain.upper()} for {where}: {what}; {effect}."
    ev = [
        Evidence(
            source=f"send:{p.domain}",
            selector=f"ENDPOINT={p.detail}",
            statistic=f"data_quality:{p.kind}",
            value=float(p.count),
            role="primary",
            direction="none",
            display=f"{p.count} record(s)",
        ),
    ]
    if p.example:
        ev.append(
            Evidence(
                source=f"send:{p.domain}",
                selector=f"ENDPOINT={p.detail}",
                statistic="data_quality:example",
                value=0.0,
                role="supporting",
                direction="none",
                display=f"for example {p.example}",
            )
        )
    return title, summary, ev


class ToxStudyMonitor:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.PRECLINICAL
    subscribes_to = frozenset({EventType.SEND_STUDY_LOADED})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = ToxStudyMonitorConfig

    def run(
        self, trigger: Event, ctx: AgentContext[ToxStudyMonitorConfig]
    ) -> Sequence[FindingDraft]:
        study, cfg = ctx.study, ctx.config
        if not isinstance(trigger.payload, SendStudyLoaded):
            raise ValueError(f"{AGENT_ID} runs on a SEND study, not on {trigger.payload.type}")
        if trigger.payload.cut_id != study.cut_id:
            raise ValueError(
                f"trigger is for SEND cut {trigger.payload.cut_id}, the reader for {study.cut_id}"
            )
        tables = _tables(study)
        analysis = analyse(tables, cfg)
        drafts = self.drafts(analysis, cfg, study.study_id, study.cut_id, tables)
        ctx.log.info(
            "tox.analysed",
            endpoints=len(analysis.summaries),
            findings=len(drafts),
            data_quality=len(analysis.problems),
        )
        return drafts

    def drafts(
        self,
        analysis: Analysis,
        cfg: ToxStudyMonitorConfig,
        study_id: str,
        cut_id: str,
        tables: Mapping[str, pl.DataFrame] | None = None,
    ) -> list[FindingDraft]:
        tables = tables or {}
        unit = analysis.design.dose_unit
        out: list[FindingDraft] = []
        for item in raise_findings(analysis, tables, cfg).items:
            if isinstance(item, EndpointItem):
                s = item.summary
                title, summary = _endpoint_draft_text(item, unit)
                evidence = [_group_evidence(s, g, unit) for g in s.groups]
                evidence.append(_trend_evidence(s, cfg))
                if item.recovery is not None:
                    evidence += _recovery_evidence(s, item.recovery, unit)
                dose_related = item.rule in (DOSE_RELATED_CHANGE, DOSE_RELATED_INCIDENCE)
                severity = Severity.MEDIUM if dose_related else Severity.LOW
                score = 1.0 - (s.trend_p if s.trend_p is not None else 1.0)
            elif isinstance(item, SecondaryItem):
                title, summary, evidence = _secondary_text(item, unit)
                severity, score = Severity.LOW, 0.3
            elif isinstance(item, NeoplasmItem):
                title, summary, evidence = _neoplasm_text(item, unit)
                severity, score = Severity.MEDIUM, 0.9
            else:
                title, summary, evidence = _death_text(item, unit)
                severity, score = Severity.HIGH, 1.0
            out.append(
                self._draft(
                    cfg, study_id, cut_id, item.check, title, summary, evidence, severity, score
                )
            )
        for p in analysis.problems:
            title, summary, evidence = _problem_text(p)
            out.append(
                self._draft(
                    cfg, study_id, cut_id, p.check, title, summary, evidence, Severity.LOW, 0.1
                )
            )
        return out

    def _draft(
        self,
        cfg: ToxStudyMonitorConfig,
        study_id: str,
        cut_id: str,
        check: str,
        title: str,
        summary: str,
        evidence: list[Evidence],
        severity: Severity,
        score: float,
    ) -> FindingDraft:
        return FindingDraft(
            dedup_key=make_dedup_key(
                agent_id=AGENT_ID,
                agent_version=VERSION,
                kind=FindingKind.TOX_SIGNAL,
                study_id=study_id,
                site_id=None,
                cut_id=cut_id,
                check=check,
            ),
            kind=FindingKind.TOX_SIGNAL,
            site_id=None,
            severity=severity,
            title=title,
            summary=summary,
            evidence=tuple(evidence),
            score=score,
            visible_to=cfg.visible_to,
        )


__all__ = ["AGENT_ID", "DATA_QUALITY", "PAIRWISE_CHANGE", "VERSION", "ToxStudyMonitor"]
