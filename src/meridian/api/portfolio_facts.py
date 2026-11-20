"""The portfolio facts, built AS the portfolio lead (phase 5 step 3b;
docs/stage-gate-notes.md). Only Repository views of a Repository whose person is the
portfolio lead are read, so the facts can hold nothing that role may not see: the
Repository applies meridian.api.visibility to every event and finding. No subject id,
no unblinded value, no hidden finding. The ledger itself is never read here."""

from collections.abc import Sequence
from pathlib import Path

from meridian.api.repository import FindingFilters, Repository
from meridian.data.stage_layout import PORTFOLIO
from meridian.domain.display import sig
from meridian.domain.enums import FindingKind, Role
from meridian.domain.events import ActorSuspended, Event, PortfolioOutlook
from meridian.portfolio import facts as fx
from meridian.portfolio.facts import Fact

# Agents whose findings have a group of their own (not open_finding).
OWN_GROUP = {
    FindingKind.DOSE_PROPOSAL,
    FindingKind.IND_READINESS,
    FindingKind.MONITORING_RECOMMENDATION,
    FindingKind.REPORTING_SIGNAL,
}
CLOSED = {"accepted", "dismissed"}
DOSE_STATUS = {"planned": "accepted", "proposed": "open", "escalated": "escalated",
               "dismissed": "dismissed"}  # fmt: skip


class NotThePortfolioLeadError(ValueError):
    pass


def build(repo: Repository) -> list[Fact]:
    if repo.role is not Role.PORTFOLIO_LEAD:
        raise NotThePortfolioLeadError(f"facts are built as the portfolio lead, not {repo.role}")
    items = repo.findings(FindingFilters(), limit=100_000, offset=0).items
    by_id = {i.finding_id: i for i in items}
    out: list[Fact] = []
    portfolio = repo.portfolio()
    preclinical = dict(repo.preclinical_assets())
    for asset in portfolio.assets:
        a = asset.asset_id
        study = preclinical.get(a)
        if study is not None:
            out += _preclinical(repo, a, study, items, by_id)
        elif a == repo.study_id:
            out += _clinical(repo, a, items, by_id)
        else:
            st = asset.static
            fields = [
                ("stage", "Stage", st.stage),
                ("indication", "Indication", st.indication),
                ("milestone", "Milestone", f"{st.milestone} ({st.milestone_due})"),
                ("progress", "Progress", f"{st.progress_percent}%"),
                ("probability_of_success", "Probability of success",
                 st.probability_of_success or "not given"),
                ("status_text", "Status (as written)", st.status.text),
            ]  # fmt: skip
            out += [
                Fact(a, "static", f"static:{k}", label, str(value), "static", static=True)
                for k, label, value in fields
            ]
    return out


def _preclinical(repo: Repository, a: str, study: str, items: Sequence, by_id: dict) -> list[Fact]:  # type: ignore[type-arg]
    out: list[Fact] = []
    for v in repo.noael_views(study):
        if v.current:
            value = (
                f"{sig(v.noael_dose)} {v.dose_unit}"
                if v.established and v.noael_dose is not None
                else "not established"
            )
            status = "established" if v.established else "not_established"
            out.append(Fact(a, "noael", f"noael:{v.sex}", f"NOAEL ({v.sex})", value, status,
                            seq=v.seq))  # fmt: skip
    sd = repo.starting_dose(study)
    if sd.status in DOSE_STATUS:
        proposal = next((c for c in sd.chain if c.step == "proposal"), None)
        decision = next((c for c in sd.chain if c.step == "decision"), None)
        link = decision or proposal
        out.append(
            Fact(a, "starting_dose", "starting_dose", "Planned first-in-human starting dose",
                 f"{sig(sd.dose_mg)} mg" if sd.dose_mg is not None else sd.why,
                 DOSE_STATUS[sd.status], seq=None if link is None else link.seq,
                 finding_id=None if proposal is None else proposal.ref)
        )  # fmt: skip
    ind = repo.ind_readiness(a)
    if ind.status == "assessed":
        out.append(
            Fact(a, "ind_readiness", "ind_readiness", "IND readiness",
                 f"{ind.percent_met:.0f}% ({ind.met} of {ind.applicable})", "assessed",
                 seq=ind.loaded_seq, number=ind.percent_met)
        )  # fmt: skip
    mb = repo.monitoring_brief(study)
    if mb.status not in ("none", "not_shown") and mb.finding_id:
        f = by_id.get(mb.finding_id)
        out.append(
            Fact(a, "monitoring", "monitoring", "First-in-human monitoring",
                 ", ".join(mb.organs) or "no organ", mb.status,
                 seq=mb.decided_seq or (f.seq if f else None), finding_id=mb.finding_id)
        )  # fmt: skip
    for f in items:
        if f.study_id not in (study, a):
            continue
        if f.kind is FindingKind.IND_READINESS:
            out.append(Fact(a, "ind_gap", f"ind_gap:{f.finding_id}", f.title, f.title,
                            f.status, f.severity.value, f.seq, f.finding_id))  # fmt: skip
        elif f.kind not in OWN_GROUP and f.status not in CLOSED:
            out.append(Fact(a, "open_finding", f"open:{f.finding_id}", f.title, f.title,
                            f.status, f.severity.value, f.seq, f.finding_id))  # fmt: skip
    return out


def _clinical(repo: Repository, a: str, items: Sequence, by_id: dict) -> list[Fact]:  # type: ignore[type-arg]
    out: list[Fact] = []
    groups: dict[tuple[str, str], list[object]] = {}
    for f in items:
        if f.study_id == a and f.status not in CLOSED:
            groups.setdefault((f.agent_id.split(".")[0], f.severity.value), []).append(f)
    for (area, severity), found in sorted(groups.items()):
        out.append(
            Fact(a, "open_findings", f"open:{area}:{severity}",
                 f"Open {severity} findings ({area})", str(len(found)), "open", severity,
                 seq=max(x.seq for x in found), number=float(len(found)))  # type: ignore[attr-defined]
        )  # fmt: skip
    out.append(Fact(a, "qtl", "qtl", "QTL status", "not shown to the portfolio lead",
                    "not_shown"))  # fmt: skip
    cuts = [c for c in repo.cuts(a) if c.scope == "operational" and c.kind == "base"]
    if cuts:
        cut = cuts[-1]
        rec = repo.reconciliation(cut.cut_id)
        runs = [
            e for e in repo.snapshot().suspended
            if isinstance(e, Event) and isinstance(e.payload, ActorSuspended)
            and e.payload.agent_id == rec.agent_id and e.payload.trigger_seq == cut.trigger_seq
        ]  # fmt: skip
        mismatches = sum(c.count for c in rec.by_check)
        out.append(
            Fact(a, "sae_recon", "sae_recon", "SAE reconciliation", cut.cut_id,
                 "run_completed" if rec.run_completed else "not_run",
                 seq=runs[-1].seq if runs else cut.trigger_seq, number=float(mismatches))
        )  # fmt: skip
    view = repo.safety_signals()
    if view.status == "scanned" and view.scan_seq is not None:
        out.append(Fact(a, "signal_scan", "signal_scan", "Safety signal scan",
                        f"{view.scan_id}, data date {view.data_date}", "scanned",
                        seq=view.scan_seq, number=float(len(view.signals))))  # fmt: skip
        for r in view.signals:
            if r.tier == "priority" and r.finding_id:
                f = by_id.get(r.finding_id)
                out.append(
                    Fact(a, "priority_signal", f"signal:{r.term}",
                         f"Priority signal: {r.term.lower()}",
                         f"PRR {r.prr:.1f}, {r.reports} reports" if r.prr else str(r.reports),
                         r.status or "open", "low", f.seq if f else view.scan_seq, r.finding_id)
                )  # fmt: skip
    return out


def previous(entries: Sequence[Event], before_seq: int) -> list[Fact]:
    """The facts of the newest PortfolioOutlook before before_seq, with a marker row
    (group _outlook, seq its own) so the agent knows which outlook it compares with."""
    outlooks = [
        e for e in entries
        if isinstance(e.payload, PortfolioOutlook) and e.seq < before_seq
    ]  # fmt: skip
    if not outlooks:
        return []
    last = outlooks[-1]
    assert isinstance(last.payload, PortfolioOutlook)
    out = [Fact("_", "_outlook", "_outlook", "previous outlook", seq=last.seq)]
    for asset in last.payload.assets:
        out += [
            Fact(asset.asset_id, f.group, f.key, f.label, f.value, f.status, seq=f.seq,
                 static=f.static)
            for f in asset.facts
        ]  # fmt: skip
    return out


def write(directory: Path, facts: Sequence[Fact], before: Sequence[Fact]) -> Path:
    area = directory / PORTFOLIO
    area.mkdir(parents=True, exist_ok=True)
    fx.frame(facts).write_parquet(area / f"{fx.FACTS_TABLE}.parquet")
    fx.frame(before).write_parquet(area / f"{fx.PREVIOUS_TABLE}.parquet")
    return area


__all__ = ["build", "previous", "write"]
