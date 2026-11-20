"""portfolio.safety_signal_scout 1.0.1: signals of disproportionate reporting for MRD-201's
drug (xanomeline, with trospium chloride) in public FAERS data through openFDA, as
pre-registered in docs/signal-scout.md (statistics in meridian.portfolio.disproportionality,
queries in meridian.portfolio.openfda).

The agent itself is pure: it reads the run's recorded openFDA responses (the
openfda_responses table) and words each signal. In the portfolio_signal sandbox,
meridian-signal-worker fetches those responses first (the only zone with egress to
api.fda.gov) and returns them with the drafts; the host keeps them, so a rerun on them
gives identical drafts. Every finding is a reporting signal, never an incidence or a
causal claim, and carries FAERS's own caveats; the medical monitor decides it.

1.0.1 (phase 5 step 3b, pre-registered in docs/signal-scout.md): openFDA's throttling is
retried; every signal carries the context (COBENFY is oral xanomeline with trospium, an
antimuscarinic, in schizophrenia; MRD-201 is transdermal xanomeline alone in Alzheimer's
disease); and each signal gets a tier. Only priority signals become findings; the rest stay
on the Safety signals view with their tier.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import polars as pl

from meridian.agents.base import AgentContext, FindingDraft, Zone
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event, SignalScanRequested
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import SignalScoutConfig
from meridian.portfolio import openfda as o
from meridian.portfolio.disproportionality import Ratio, SignalRule, Table, chi2_yates, prr, ror

AGENT_ID = "portfolio.safety_signal_scout"
VERSION = "1.0.1"  # 1.0.0 phase 5 step 3a
VISIBLE_TO_MAX = frozenset({Role.MEDICAL_MONITOR, Role.PORTFOLIO_LEAD})
CHECK = "reporting_signal"
Tier = Literal["listed", "indication", "use", "weak", "priority", "not_signal"]
TIERS: tuple[Tier, ...] = ("listed", "indication", "use", "weak", "priority")


def responses_from(frame: pl.DataFrame) -> list[o.Response]:
    return [
        o.Response(
            path=str(r["path"]),
            query=str(r["query"]),
            status=int(r["status"]),
            body=str(r["body"]),
            sha256=str(r["sha256"]),
            last_updated=str(r["last_updated"]),
        )
        for r in frame.iter_rows(named=True)
    ]


def responses_frame(responses: Sequence[o.Response]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "path": [r.path for r in responses],
            "query": [r.query for r in responses],
            "status": [r.status for r in responses],
            "body": [r.body for r in responses],
            "sha256": [r.sha256 for r in responses],
            "last_updated": [r.last_updated for r in responses],
        },
        schema={
            "path": pl.String,
            "query": pl.String,
            "status": pl.Int64,
            "body": pl.String,
            "sha256": pl.String,
            "last_updated": pl.String,
        },
    )


@dataclass(frozen=True, slots=True)
class TermResult:
    term: str
    table: Table
    prr: Ratio | None
    ror: Ratio | None
    chi2: float
    signal: bool
    listed: bool | None  # None: no label to compare with
    sha256: str  # the term total's response
    tier: Tier = "not_signal"


def tier_of(
    term: str, signal: bool, listed: bool | None, a: int, p: Ratio | None, cfg: SignalScoutConfig
) -> Tier:
    """The 1.0.1 triage: the first tier that applies (docs/signal-scout.md)."""
    if not signal:
        return "not_signal"
    if listed:
        return "listed"
    if term.upper() in {t.upper() for t in cfg.indication_terms}:
        return "indication"
    if term.upper() in {t.upper() for t in cfg.use_terms}:
        return "use"
    if p is None or p.low <= cfg.priority_prr_low_above or a < cfg.priority_min_reports:
        return "weak"
    return "priority"


@dataclass(frozen=True, slots=True)
class Scan:
    data_date: str
    drug_reports: int
    all_reports: int
    terms_with_drug: int
    tested: tuple[TermResult, ...]
    not_answered: tuple[str, ...]  # terms whose total query failed or was not made
    label: o.Label | None
    reactions_sha256: str


def analyse(responses: Sequence[o.Response], cfg: SignalScoutConfig) -> Scan:
    by_query = {(r.path, r.query): r for r in responses}

    def get(path: str, query: str) -> o.Response | None:
        return by_query.get((path, query))

    reactions = get(o.EVENT, o.reactions_query(cfg))
    drug = get(o.EVENT, o.drug_total_query(cfg))
    everyone = get(o.EVENT, o.all_total_query(cfg))
    if reactions is None or drug is None or everyone is None:
        raise ValueError("the recorded responses lack the reaction, drug or total query")
    n_drug, n_all = o.total(drug), o.total(everyone)
    if n_drug is None or n_all is None or reactions.status != 200:
        raise ValueError("openFDA did not answer the reaction, drug or total query")
    label_r = get(o.LABEL, o.label_query(cfg))
    label = o.label_of(label_r) if label_r is not None else None
    rule = SignalRule(cfg.min_reports, cfg.prr_min, cfg.chi2_min)
    tested: list[TermResult] = []
    missing: list[str] = []
    pairs = o.counts(reactions)
    for term, a in sorted(pairs, key=lambda x: (-x[1], x[0])):
        if a < cfg.min_reports:
            continue
        r = get(o.EVENT, o.term_total_query(cfg, term))
        n_term = None if r is None else o.total(r)
        if r is None or n_term is None:
            missing.append(term)
            continue
        t = Table.from_counts(a, n_drug, n_term, n_all)
        signal = rule.is_signal(t)
        on_label = (
            None
            if label is None
            else o.listed(term, label, cfg.label_spellings, cfg.label_synonyms)
        )
        p = prr(t)
        tested.append(
            TermResult(
                term=term,
                table=t,
                prr=p,
                ror=ror(t),
                chi2=chi2_yates(t),
                signal=signal,
                listed=on_label,
                sha256=r.sha256,
                tier=tier_of(term, signal, on_label, a, p, cfg),
            )
        )
    return Scan(
        data_date=reactions.last_updated,
        drug_reports=n_drug,
        all_reports=n_all,
        terms_with_drug=len(pairs),
        tested=tuple(tested),
        not_answered=tuple(missing),
        label=label,
        reactions_sha256=reactions.sha256,
    )


def _ratio(name: str, r: Ratio | None) -> str:
    if r is None:
        return f"{name} undefined"
    return f"{name} {r.value:.2f} (95% CI {r.low:.2f} to {r.high:.2f})"


TIER_TEXT: dict[str, str] = {
    "listed": "named in the US label",
    "indication": "an indication term (confounded by indication)",
    "use": "a term about use, effect or supply, not a clinical event",
    "weak": "weak: lower 95% limit of the PRR at most {low:g} or fewer than {n} reports",
    "priority": "priority: lower 95% limit of the PRR above {low:g}, at least {n} reports, "
    "not an indication or use term, not in the label",
    "not_signal": "not a signal",
}


def tier_text(tier: str, cfg: SignalScoutConfig) -> str:
    return TIER_TEXT[tier].format(low=cfg.priority_prr_low_above, n=cfg.priority_min_reports)


def evidence(t: TermResult, scan: Scan, cfg: SignalScoutConfig) -> list[Evidence]:
    """A signal's evidence (also the Safety signals view's, for every tier)."""
    return _evidence(t, scan, cfg)


def _evidence(t: TermResult, scan: Scan, cfg: SignalScoutConfig) -> list[Evidence]:
    tab = t.table
    sel = f"TERM={t.term}; DATA_DATE={scan.data_date}"
    out = [
        Evidence(
            source="openfda:drug/event",
            selector=sel,
            statistic="signal:a",
            value=float(tab.a),
            threshold=float(cfg.min_reports),
            role="primary",
            direction="high",
            display=f"{tab.a} reports with the drug and {t.term} (at least {cfg.min_reports})",
        ),
        Evidence(
            source="openfda:drug/event",
            selector=sel,
            statistic="signal:table",
            value=float(tab.n),
            role="supporting",
            direction="none",
            display=(
                f"2 x 2 table of reports: a = {tab.a}, b = {tab.b} (drug, other terms), "
                f"c = {tab.c} ({t.term}, other drugs), d = {tab.d} (neither); "
                f"{scan.drug_reports} reports with the drug, {scan.all_reports} in FAERS"
            ),
        ),
        Evidence(
            source="openfda:drug/event",
            selector=sel,
            statistic="signal:prr",
            value=t.prr.value if t.prr else 0.0,
            threshold=cfg.prr_min,
            role="primary",
            direction="high",
            display=_ratio("PRR", t.prr) + f" (at least {cfg.prr_min:g})",
        ),
        Evidence(
            source="openfda:drug/event",
            selector=sel,
            statistic="signal:chi2",
            value=t.chi2,
            threshold=cfg.chi2_min,
            role="primary",
            direction="high",
            display=f"chi-squared {t.chi2:.1f}, Yates-corrected (at least {cfg.chi2_min:g})",
        ),
        Evidence(
            source="openfda:drug/event",
            selector=sel,
            statistic="signal:ror",
            value=t.ror.value if t.ror else 0.0,
            role="supporting",
            direction="none",
            display=_ratio("ROR", t.ror) + " (reported, not part of the rule)",
        ),
        Evidence(
            source="openfda:drug/event",
            selector=f"{sel}; SHA256={t.sha256}",
            statistic="signal:query",
            value=0.0,
            role="supporting",
            direction="none",
            display=(
                f"openFDA data date {scan.data_date}; product "
                f'{cfg.product_field}:"{cfg.product_term}"; term total sha256 {t.sha256[:16]}'
            ),
        ),
    ]
    if scan.label is not None:
        out.append(
            Evidence(
                source="openfda:drug/label",
                selector=f"SET_ID={scan.label.set_id}; VERSION={scan.label.version}",
                statistic="signal:label_listed",
                value=1.0 if t.listed else 0.0,
                role="supporting",
                direction="none",
                display=(
                    f"US label ({scan.label.set_id}, version {scan.label.version}, effective "
                    f"{scan.label.effective_time}): "
                    + ("listed" if t.listed else "not found")
                    + " in its adverse reactions, warnings and boxed warning text (exact "
                    "phrase, no synonyms)"
                ),
            )
        )
    out += [
        Evidence(
            source=cfg.caveats_source,
            selector=f"CAVEAT={i}",
            statistic="signal:caveat",
            value=0.0,
            role="supporting",
            direction="none",
            display=f'FAERS caveat: "{c}"',
        )
        for i, c in enumerate(cfg.caveats, start=1)
    ]
    # 1.0.1: the tier, the context of these reports against MRD-201, and on an indication
    # term the confounding.
    out.append(
        Evidence(
            source="docs/signal-scout.md (1.0.1 triage)",
            selector=sel,
            statistic="signal:tier",
            value=float(TIERS.index(t.tier)) if t.tier in TIERS else -1.0,
            role="supporting",
            direction="none",
            display=f"Tier: {tier_text(t.tier, cfg)}",
        )
    )
    out += [
        Evidence(
            source="openfda:drug/label; config/portfolio.toml",
            selector=f"CONTEXT={i}",
            statistic="signal:context",
            value=0.0,
            role="supporting",
            direction="none",
            display=c,
        )
        for i, c in enumerate(cfg.context, start=1)
    ]
    if t.tier == "indication" and cfg.indication_caveat:
        out.append(
            Evidence(
                source=cfg.indication_source or "docs/signal-scout.md",
                selector=sel,
                statistic="signal:indication",
                value=0.0,
                role="supporting",
                direction="none",
                display=cfg.indication_caveat,
            )
        )
    return out


class SafetySignalScout:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.PORTFOLIO
    subscribes_to = frozenset({EventType.SIGNAL_SCAN_REQUESTED})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = SignalScoutConfig

    def run(self, trigger: Event, ctx: AgentContext[SignalScoutConfig]) -> Sequence[FindingDraft]:
        if not isinstance(trigger.payload, SignalScanRequested):
            raise ValueError(f"{AGENT_ID} wakes on a signal scan, not {trigger.payload.type}")
        if trigger.payload.scan_id != ctx.study.cut_id:
            raise ValueError("the trigger's scan is not the reader's")
        scan = analyse(responses_from(ctx.study.table(o.RESPONSES_TABLE)), ctx.config)
        drafts = self.drafts(scan, ctx.config, ctx.study.cut_id)
        ctx.log.info(
            "signals.scanned",
            data_date=scan.data_date,
            tested=len(scan.tested),
            signals=len(drafts),
            not_answered=len(scan.not_answered),
        )
        return drafts

    def drafts(self, scan: Scan, cfg: SignalScoutConfig, cut_id: str) -> list[FindingDraft]:
        out = []
        for t in scan.tested:
            if t.tier != "priority":
                continue
            p = t.prr
            title = (
                f"Reporting signal for {cfg.drug}: {t.term.lower()} "
                f"(PRR {p.value:.1f}, {t.table.a} reports)"
                if p
                else f"Reporting signal for {cfg.drug}: {t.term.lower()}"
            )
            summary = (
                f"In FAERS (openFDA, data date {scan.data_date}), {t.term.lower()} is reported "
                f"with {cfg.drug} in {t.table.a} of its {scan.drug_reports} reports, "
                "disproportionately often against all other drugs: "
                f"{_ratio('PRR', t.prr)}, chi-squared {t.chi2:.1f}, {_ratio('ROR', t.ror)}. "
                "This is a signal of disproportionate reporting (Evans et al. 2001: at least "
                f"{cfg.min_reports} reports, PRR at least {cfg.prr_min:g}, chi-squared at "
                f"least {cfg.chi2_min:g}), not an incidence and not evidence that the drug "
                "causes it. It is a priority signal (lower 95% limit of the PRR above "
                f"{cfg.priority_prr_low_above:g}, at least {cfg.priority_min_reports} reports, "
                "not an indication or use term). These reports concern oral xanomeline with "
                "trospium in schizophrenia; MRD-201 is transdermal xanomeline alone in "
                "Alzheimer's disease."
                + (
                    " The US label "
                    + ("lists it" if t.listed else "does not name it in these words")
                    + "."
                    if t.listed is not None
                    else ""
                )
                + " The medical monitor decides what it means for MRD-201."
            )
            out.append(
                FindingDraft(
                    dedup_key=make_dedup_key(
                        agent_id=AGENT_ID,
                        agent_version=VERSION,
                        kind=FindingKind.REPORTING_SIGNAL,
                        study_id=cfg.study_id,
                        site_id=None,
                        cut_id=cut_id,
                        check=f"{CHECK}:{t.term}",
                    ),
                    kind=FindingKind.REPORTING_SIGNAL,
                    site_id=None,
                    severity=Severity.LOW,
                    title=title,
                    summary=summary,
                    evidence=tuple(_evidence(t, scan, cfg)),
                    score=min(1.0, (p.value if p else 0.0) / 10),
                    visible_to=cfg.visible_to,
                )
            )
        return out


__all__ = [
    "AGENT_ID",
    "VERSION",
    "SafetySignalScout",
    "analyse",
    "evidence",
    "responses_frame",
    "tier_of",
]
