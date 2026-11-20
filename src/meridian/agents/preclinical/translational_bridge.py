"""preclinical.translational_bridge 1.0.0: how MRD-101's first-in-human study should monitor
the organs its NOAEL rests on, and the safety margins, as pre-registered in
docs/translational-notes.md (rules in meridian.preclinical.bridge; the mapping is
config/translational_monitoring.toml, every item quoting its source).

Deterministic: the phase 3 design planned it on the local model, but mapping a target
organ to standard clinical monitoring is a rule with sources, not a judgement. It wakes on
the latest NOAEL decision or the acceptance of a dose proposal (the host stages the
decisions, the findings they cite and the current proposal in /stage/noael). One
MonitoringRecommendation finding: an item per target organ with the findings it rests on,
organs with no mapping entry reported as unmapped, the NOAEL and LOAEL as human equivalent
doses against the planned starting dose. It proposes; the medical monitor decides.
"""

from collections.abc import Sequence

import polars as pl

from meridian.agents.base import AgentContext, FindingDraft, Zone
from meridian.agents.preclinical.dose_translation import (
    StagedDecision,
    basis_of,
    current_decisions,
    staged_decisions,
    study_design,
)
from meridian.data.study_store import MissingTableError, StudyReader
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event, HumanDecision, NoaelDecision
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import TranslationalBridgeConfig
from meridian.preclinical.bridge import (
    CitedFinding,
    Proposal,
    Recommendation,
    cited_findings,
    current_proposal,
    recommend,
)
from meridian.preclinical.dose import sig
from meridian.preclinical.noael_stage import BRIDGE_FINDINGS, BRIDGE_PROPOSAL, NOAEL_TABLE

AGENT_ID = "preclinical.translational_bridge"
VERSION = "1.0.0"
VISIBLE_TO_MAX = frozenset(
    {Role.TOXICOLOGIST, Role.MEDICAL_MONITOR, Role.PORTFOLIO_LEAD, Role.QA_AUDITOR}
)
CHECK = "monitoring"
DECIDES = (
    " This is a proposal, not a decision: the medical monitor accepts, escalates or dismisses it."
)


def _table(study: StudyReader, name: str) -> pl.DataFrame | None:
    try:
        return study.table(name)
    except MissingTableError:
        return None


def _finding_evidence(f: CitedFinding, organ: str | None) -> Evidence:
    where = organ if organ is not None else "UNMAPPED"
    return Evidence(
        source="ledger:finding_raised",
        selector=f"FINDING={f.finding_id}; ORGAN={where}; CITED_BY={','.join(f.cited_by)}",
        statistic="monitoring:finding" if organ is not None else "monitoring:unmapped",
        value=0.0,
        role="primary" if organ is not None else "supporting",
        direction="none",
        display=(
            f"{f.finding_id} ({organ}): {f.title}"
            if organ is not None
            else f"{f.finding_id}, unmapped (no mapping entry for {f.check}): {f.title}"
        ),
    )


def _evidence(
    rec: Recommendation,
    basis: StagedDecision,
    proposal: Proposal | None,
    cfg: TranslationalBridgeConfig,
) -> list[Evidence]:
    out: list[Evidence] = []
    for item in rec.items:
        o = item.organ
        out.append(
            Evidence(
                source="config:translational_monitoring",
                selector=f"ORGAN={o.id}",
                statistic=f"monitoring:organ:{o.id}",
                value=float(len(item.findings)),
                role="primary",
                direction="none",
                display=f"{o.label}: target organ of {len(item.findings)} cited finding(s)",
            )
        )
        for field, text in (
            ("measure", o.measure),
            ("frequency", o.frequency),
            ("review", o.review),
            ("stopping", o.stopping),
        ):
            out.append(
                Evidence(
                    source="config:translational_monitoring",
                    selector=f"ORGAN={o.id}",
                    statistic=f"monitoring:{field}:{o.id}",
                    value=0.0,
                    role="primary",
                    direction="none",
                    display=f"{o.label}, {field}: {text}",
                )
            )
        for s in o.sources:
            out.append(
                Evidence(
                    source="config:translational_monitoring",
                    selector=f"ORGAN={o.id}; SOURCE={s.source}; SECTION={s.section}",
                    statistic=f"monitoring:source:{o.id}",
                    value=0.0,
                    role="supporting",
                    direction="none",
                    display=f'{s.source}, {s.section}: "{s.quote}"',
                )
            )
        out += [_finding_evidence(f, o.id) for f in item.findings]
    out += [_finding_evidence(f, None) for f in rec.unmapped]
    for m in rec.margins:
        name = m.label.lower()
        divisor = m.animal_dose / m.hed_mg_per_kg if m.hed_mg_per_kg else 0.0
        out.append(
            Evidence(
                source="ledger:noael_decision",
                selector=f"DECISION={basis.decision_id}; SEX={basis.sex}; SEQ={basis.seq}",
                statistic=f"monitoring:{name}_hed_mg",
                value=m.hed_mg,
                threshold=rec.human_weight_kg,
                role="supporting",
                direction="none",
                display=(
                    f"{m.label} {m.animal_dose:g} {m.dose_unit} as a human equivalent dose: "
                    f"{m.animal_dose:g} / {divisor:.2g} = {sig(m.hed_mg_per_kg)} mg/kg, "
                    f"{sig(m.hed_mg)} mg for a {rec.human_weight_kg:g} kg adult (FDA 2005, the "
                    "dose proposal's factor)"
                ),
            )
        )
        if m.ratio is not None and rec.starting_dose_mg is not None:
            out.append(
                Evidence(
                    source="ledger:finding_raised",
                    selector=f"PROPOSAL={proposal.finding_id if proposal else ''}",
                    statistic=f"monitoring:{name}_margin",
                    value=m.ratio,
                    role="primary",
                    direction="none",
                    display=(
                        f"{m.label} margin: {sig(m.hed_mg)} mg / {sig(rec.starting_dose_mg)} mg "
                        f"starting dose = {m.ratio:.3g} x"
                    ),
                )
            )
    if rec.margin_error is not None:
        out.append(
            Evidence(
                source="config:agents.dose_translation",
                selector=f"DECISION={basis.decision_id}",
                statistic="monitoring:no_margin",
                value=0.0,
                role="supporting",
                direction="none",
                display=f"No margin: {rec.margin_error}",
            )
        )
    out.append(_proposal_evidence(proposal, rec))
    b = cfg.mapping.basis
    out.append(
        Evidence(
            source="config:translational_monitoring",
            selector=f"SOURCE={b.source}; SECTION={b.section}",
            statistic="monitoring:basis",
            value=0.0,
            role="supporting",
            direction="none",
            display=(
                f"Why nonclinical findings shape Phase 1 monitoring: {b.source}, "
                f'{b.section}: "{b.quote}"'
            ),
        )
    )
    return out


def _proposal_evidence(proposal: Proposal | None, rec: Recommendation) -> Evidence:
    if proposal is None or rec.starting_dose_mg is None:
        return Evidence(
            source="ledger:finding_raised",
            selector="PROPOSAL=none",
            statistic="monitoring:starting_dose_mg",
            value=0.0,
            role="supporting",
            direction="none",
            display=(
                "No planned starting dose (no dose proposal with a starting dose): margins "
                "are given as human equivalent doses only"
            ),
        )
    state = (
        f"accepted by {proposal.decided_by}"
        if proposal.status == "accepted"
        else f"{proposal.status}, not accepted"
    )
    return Evidence(
        source="ledger:finding_raised",
        selector=f"PROPOSAL={proposal.finding_id}; SEQ={proposal.seq}; STATUS={proposal.status}",
        statistic="monitoring:starting_dose_mg",
        value=rec.starting_dose_mg,
        role="supporting",
        direction="none",
        display=(
            f"Planned starting dose {sig(rec.starting_dose_mg)} mg ({proposal.finding_id}, {state})"
        ),
    )


class TranslationalBridge:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.PRECLINICAL
    # The runner lists under HUMAN_DECISION the latest of a study's NOAEL decision and the
    # acceptances of its dose proposals (AgentRunner._bridge_triggers).
    subscribes_to = frozenset({EventType.HUMAN_DECISION})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = TranslationalBridgeConfig

    def run(
        self, trigger: Event, ctx: AgentContext[TranslationalBridgeConfig]
    ) -> Sequence[FindingDraft]:
        if not isinstance(trigger.payload, NoaelDecision | HumanDecision):
            raise ValueError(f"{AGENT_ID} wakes on a NOAEL decision or an acceptance")
        study, cfg = ctx.study, ctx.config
        frame = _table(study, NOAEL_TABLE)
        decisions = [
            d
            for d in (staged_decisions(frame) if frame is not None else [])
            if d.study_id == study.study_id and d.seq <= trigger.seq
        ]
        current = current_decisions(decisions)
        basis = basis_of(current)
        if basis is None:
            ctx.log.info("bridge.no_decision")
            return []
        found = _table(study, BRIDGE_FINDINGS)
        findings = cited_findings(found) if found is not None else []
        prop = _table(study, BRIDGE_PROPOSAL)
        proposal = current_proposal(prop) if prop is not None else None
        draft = self.draft(basis, findings, proposal, study, cfg)
        ctx.log.info("bridge.recommended", check=draft.dedup_key.split("|")[-1])
        return [draft]

    def draft(
        self,
        basis: StagedDecision,
        findings: Sequence[CitedFinding],
        proposal: Proposal | None,
        study: StudyReader,
        cfg: TranslationalBridgeConfig,
    ) -> FindingDraft:
        species = study_design(study).species
        rec = recommend(
            findings,
            basis.noael_dose,
            basis.loael_dose,
            basis.dose_unit,
            species,
            proposal,
            cfg.mapping,
            cfg.dose,
        )
        organs = ", ".join(i.organ.label.split(" (")[0].lower() for i in rec.items)
        title = (
            f"Monitoring recommendation for {cfg.asset_id}'s first-in-human study: {organs}"
            if rec.items
            else f"Monitoring recommendation for {cfg.asset_id}'s first-in-human study: no "
            "target organ in the NOAEL's findings"
        )
        parts = [
            f"{i.organ.label}: {i.organ.measure} ({', '.join(f.finding_id for f in i.findings)})"
            for i in rec.items
        ]
        margins = "; ".join(
            f"{m.label} {m.animal_dose:g} {m.dose_unit} = {sig(m.hed_mg)} mg human equivalent"
            + (f", {m.ratio:.3g} x the starting dose" if m.ratio is not None else "")
            for m in rec.margins
        )
        start = (
            f"the planned starting dose of {sig(rec.starting_dose_mg)} mg ({proposal.finding_id}, "
            + ("accepted" if proposal.status == "accepted" else f"{proposal.status}, not accepted")
            + ")"
            if rec.starting_dose_mg is not None and proposal is not None
            else "no planned starting dose"
        )
        unmapped = (
            " Unmapped (no mapping entry, no rule given): "
            + ", ".join(f"{f.finding_id} ({f.title})" for f in rec.unmapped)
            + "."
            if rec.unmapped
            else ""
        )
        summary = (
            f"From the current NOAEL decisions ({basis.decision_id} the basis of the margins) "
            "and the findings they cite: "
            + ("; ".join(parts) if parts else "no finding maps to an organ")
            + f".{unmapped} Margins against {start}: "
            + (margins or "none")
            + ". Each item quotes its source; where a source sets no frequency or stopping "
            "rule the item says so." + DECIDES
        )
        return FindingDraft(
            dedup_key=make_dedup_key(
                agent_id=AGENT_ID,
                agent_version=VERSION,
                kind=FindingKind.MONITORING_RECOMMENDATION,
                study_id=study.study_id,
                site_id=None,
                cut_id=study.cut_id,
                check=f"{CHECK}:{basis.decision_id}:"
                f"{proposal.finding_id if proposal is not None else 'none'}",
            ),
            kind=FindingKind.MONITORING_RECOMMENDATION,
            site_id=None,
            severity=Severity.MEDIUM,
            title=title,
            summary=summary,
            evidence=tuple(_evidence(rec, basis, proposal, cfg)),
            score=0.5,
            visible_to=cfg.visible_to,
        )


__all__ = ["AGENT_ID", "VERSION", "TranslationalBridge"]
