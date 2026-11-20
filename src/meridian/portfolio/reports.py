"""Reports as drafts (phase 5 step 3b). An agent returns FindingDrafts through every
executor; a report agent returns one draft of a report kind (portfolio_outlook,
gate_pack) whose single evidence item holds the report body as canonical JSON. The runner
decodes it into a PortfolioOutlook or GatePack event; it never becomes a finding. So the
worker documents, the drafts hash and a replay's identity are the same as for findings."""

import json

from meridian.agents.base import FindingDraft
from meridian.domain.enums import FindingKind, Severity
from meridian.domain.events import GatePack, PortfolioOutlook
from meridian.domain.findings import Evidence
from meridian.ledger.hashing import canonical_json

REPORT_KINDS = frozenset({FindingKind.PORTFOLIO_OUTLOOK, FindingKind.GATE_PACK})
BODY = "report:body"


def draft(
    kind: FindingKind, dedup_key: str, title: str, summary: str, body: dict[str, object]
) -> FindingDraft:
    assert kind in REPORT_KINDS
    return FindingDraft(
        dedup_key=dedup_key,
        kind=kind,
        site_id=None,
        severity=Severity.LOW,
        title=title,
        summary=summary,
        evidence=(
            Evidence(
                source="portfolio_facts",
                selector="REPORT",
                statistic=BODY,
                value=0.0,
                display=canonical_json(body),
            ),
        ),
        score=0.0,
    )


def payload(
    d: FindingDraft, agent_id: str, agent_version: str, trigger_seq: int
) -> PortfolioOutlook | GatePack:
    """The report event of a report draft."""
    (e,) = [x for x in d.evidence if x.statistic == BODY]
    body = json.loads(e.display or "{}")
    common = {
        "agent_id": agent_id,
        "agent_version": agent_version,
        "trigger_seq": trigger_seq,
        "dedup_key": d.dedup_key,
        "summary": d.summary,
    }
    if d.kind is FindingKind.PORTFOLIO_OUTLOOK:
        return PortfolioOutlook.model_validate({**body, **common})
    if d.kind is FindingKind.GATE_PACK:
        return GatePack.model_validate({**body, **common})
    raise ValueError(f"{d.kind} is not a report kind")
