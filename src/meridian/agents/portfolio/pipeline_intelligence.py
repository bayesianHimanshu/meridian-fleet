"""portfolio.pipeline_intelligence 1.1.0 (phase 5 step 3b; 1.1.0 at the wrap-up: at most
5 risks per asset by severity then recency, the rest counted, and open findings counted
by severity instead of listed; meridian.portfolio.outlook): per asset a status, the top
risks with their ledger seqs, and what changed since the previous outlook.
Deterministic. Its input is the portfolio facts, built by the host as the portfolio lead
may see them (meridian.api.portfolio_facts), and the previous outlook's facts. Its output
is a PortfolioOutlook event, not a finding: nobody decides it."""

from collections.abc import Sequence

from meridian.agents.base import AgentContext, FindingDraft, Zone
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Visibility
from meridian.domain.events import Event, OutlookFact, PortfolioReviewRequested
from meridian.platform.agent_settings import PipelineIntelligenceConfig
from meridian.portfolio import facts as fx
from meridian.portfolio.outlook import outlook
from meridian.portfolio.reports import draft

AGENT_ID = "portfolio.pipeline_intelligence"
VERSION = "1.1.0"
VISIBLE_TO_MAX = frozenset({Role.PORTFOLIO_LEAD, Role.QA_AUDITOR})
PORTFOLIO_STUDY = "PORTFOLIO"


class PipelineIntelligence:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.PORTFOLIO
    subscribes_to = frozenset({EventType.PORTFOLIO_REVIEW_REQUESTED})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = PipelineIntelligenceConfig

    def run(
        self, trigger: Event, ctx: AgentContext[PipelineIntelligenceConfig]
    ) -> Sequence[FindingDraft]:
        p = trigger.payload
        if not isinstance(p, PortfolioReviewRequested):
            raise ValueError(f"{AGENT_ID} wakes on a portfolio review, not {p.type}")
        if p.review_id != ctx.study.cut_id:
            raise ValueError("the trigger's review is not the reader's")
        facts = fx.from_frame(ctx.study.table(fx.FACTS_TABLE))
        previous = fx.from_frame(ctx.study.table(fx.PREVIOUS_TABLE))
        prev_seq = max(
            (f.seq for f in previous if f.group == "_outlook" and f.seq is not None),
            default=None,
        )
        before = [f for f in previous if f.group != "_outlook"]
        assets = sorted({f.asset_id for f in facts})
        out = []
        for asset in assets:
            static_text = next(
                (f.value for f in facts if f.asset_id == asset and f.key == "static:status_text"),
                "",
            )
            mine_before = (
                None
                if prev_seq is None
                else [
                    OutlookFact(key=f.key, group=f.group, label=f.label, value=f.value,
                                status=f.status, seq=f.seq, static=f.static)
                    for f in before
                    if f.asset_id == asset
                ]
            )  # fmt: skip
            out.append(outlook(asset, facts, mine_before, static_text))
        counts = {s: sum(1 for a in out if a.status == s) for s in ("at_risk", "attention")}
        summary = (
            f"Outlook of {len(out)} assets for the portfolio lead: "
            + ", ".join(f"{a.asset_id} {a.status.replace('_', ' ')}" for a in out)
            + f". {counts['at_risk']} at risk, {counts['attention']} need attention. Facts "
            "are what the portfolio lead may see, each with its ledger seq; static fields "
            "are marked static. Information only: nothing here is decided."
        )
        body: dict[str, object] = {
            "review_id": p.review_id,
            "previous_outlook_seq": prev_seq,
            "assets": [a.model_dump(mode="json") for a in out],
        }
        ctx.log.info("outlook.built", assets=len(out), facts=len(facts))
        return [
            draft(
                FindingKind.PORTFOLIO_OUTLOOK,
                make_dedup_key(
                    agent_id=AGENT_ID,
                    agent_version=VERSION,
                    kind=FindingKind.PORTFOLIO_OUTLOOK,
                    study_id=PORTFOLIO_STUDY,
                    site_id=None,
                    cut_id=p.review_id,
                    check="outlook",
                ),
                f"Portfolio outlook {p.review_id}",
                summary,
                body,
            )
        ]
