"""portfolio.stage_gate_pack 1.0.0 (phase 5 step 3b; docs/stage-gate-notes.md): the
evidence pack of one gate of one asset. Each criterion of config/gates.toml met, not met or
not applicable, with the ledger seqs behind it, and the open items. Deterministic, on the
portfolio facts (built as the portfolio lead sees them). It never says go or no-go: the
portfolio lead records a GateDecision. Its output is a GatePack event, not a finding."""

from collections.abc import Sequence

from meridian.agents.base import AgentContext, FindingDraft, Zone
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Visibility
from meridian.domain.events import CriterionResult, Event, GateRequest, OpenItem
from meridian.platform.agent_settings import StageGatePackConfig
from meridian.portfolio import facts as fx
from meridian.portfolio.gates import evaluate
from meridian.portfolio.reports import draft

AGENT_ID = "portfolio.stage_gate_pack"
VERSION = "1.0.0"
VISIBLE_TO_MAX = frozenset({Role.PORTFOLIO_LEAD, Role.QA_AUDITOR})


class StageGatePack:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.PORTFOLIO
    subscribes_to = frozenset({EventType.GATE_REQUEST})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = StageGatePackConfig

    def run(self, trigger: Event, ctx: AgentContext[StageGatePackConfig]) -> Sequence[FindingDraft]:
        p = trigger.payload
        if not isinstance(p, GateRequest):
            raise ValueError(f"{AGENT_ID} wakes on a gate request, not {p.type}")
        if p.gate_request_id != ctx.study.cut_id:
            raise ValueError("the trigger's gate request is not the reader's")
        gate = ctx.config.gates.get(p.asset_id, p.gate)
        if gate is None:
            raise ValueError(f"no gate {p.gate} for {p.asset_id} in config/gates.toml")
        results = evaluate(gate, fx.from_frame(ctx.study.table(fx.FACTS_TABLE)))
        criteria = [
            CriterionResult(
                id=r.criterion.id,
                title=r.criterion.title,
                basis=r.criterion.basis,
                source=r.criterion.source,
                section=r.criterion.section,
                quote=r.criterion.quote,
                evidence=r.criterion.evidence,
                status=r.status,
                explanation=r.explanation,
                seqs=r.seqs,
            )
            for r in results
        ]
        items = [
            OpenItem(criterion_id=r.criterion.id, text=i.text, seq=i.seq, finding_id=i.finding_id)
            for r in results
            for i in r.open_items
        ]
        n = {
            s: sum(1 for c in criteria if c.status == s)
            for s in ("met", "not_met", "not_applicable")
        }
        summary = (
            f"Evidence pack for {p.asset_id}, {gate.title}: {n['met']} criteria met, "
            f"{n['not_met']} not met, {n['not_applicable']} not applicable; "
            f"{len(items)} open item(s). The pack states the evidence only; the portfolio "
            "lead decides go, conditional go or no-go."
        )
        body: dict[str, object] = {
            "gate_request_id": p.gate_request_id,
            "asset_id": p.asset_id,
            "gate": p.gate,
            "gate_title": gate.title,
            "criteria": [c.model_dump(mode="json") for c in criteria],
            "open_items": [i.model_dump(mode="json") for i in items],
        }
        return [
            draft(
                FindingKind.GATE_PACK,
                make_dedup_key(
                    agent_id=AGENT_ID,
                    agent_version=VERSION,
                    kind=FindingKind.GATE_PACK,
                    study_id=p.asset_id,
                    site_id=None,
                    cut_id=p.gate_request_id,
                    check=f"gate:{p.gate}",
                ),
                f"Evidence pack: {p.asset_id} {gate.title}",
                summary,
                body,
            )
        ]
