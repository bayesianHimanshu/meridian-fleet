"""The pipeline outlook's rules (phase 5 step 3b; docs/stage-gate-notes.md, "The
outlook"): per asset its risks with their seqs, a status, and the changes since the
previous outlook. Pure: facts in, AssetOutlook out.

Wrap-up (pipeline_intelligence 1.1.0): at most TOP_RISKS risks, most severe first and
then the newest, with the number left out (more_risks); open findings are counted by
severity instead of listed one by one (open_findings). for_display shows a recorded
outlook of either version the same way, with doses at 3 significant figures."""

from collections.abc import Sequence

from meridian.domain.display import display_doses
from meridian.domain.enums import Severity
from meridian.domain.events import AssetOutlook, OutlookFact, OutlookRisk, PortfolioOutlook
from meridian.portfolio.facts import OPEN, Fact

TOP_RISKS = 5
OPEN_FINDING = "open_finding"  # one fact per open finding (the agents' input; 1.0.0 output)
OPEN_COUNT = "open_findings"  # one fact per severity (or area and severity) with a count
_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


def by_severity_then_recency(r: OutlookRisk) -> tuple[int, int, int, str]:
    """Most severe first; within a severity a risk with no seq (something missing) first,
    then the newest (highest seq)."""
    return (_ORDER[r.severity], 1 if r.seqs else 0, -max(r.seqs, default=0), r.text)


def _risk(text: str, severity: Severity, rows: Sequence[Fact]) -> OutlookRisk:
    return OutlookRisk(
        text=text,
        severity=severity,
        seqs=tuple(sorted({r.seq for r in rows if r.seq is not None})),
    )


def risks(facts: Sequence[Fact]) -> list[OutlookRisk]:
    def group(g: str) -> list[Fact]:
        return [f for f in facts if f.group == g]

    out: list[OutlookRisk] = []
    preclinical = bool(group("ind_readiness") or group("starting_dose") or group("monitoring"))
    if preclinical and not group("noael"):
        out.append(_risk("No NOAEL decision is recorded", Severity.HIGH, []))
    for f in group("open_finding"):
        if f.severity == "high" and f.status in OPEN:
            out.append(
                _risk(f"High-severity finding {f.finding_id}: {f.label} ({f.status})",
                      Severity.HIGH, [f])
            )  # fmt: skip
    for g, what in (
        ("starting_dose", "dose proposal"),
        ("monitoring", "monitoring recommendation"),
    ):
        for f in group(g):
            if f.status != "accepted":
                out.append(
                    _risk(f"The {what} {f.finding_id or ''} is {f.status or 'not decided'}",
                          Severity.MEDIUM, [f])
                )  # fmt: skip
    gaps = [f for f in group("ind_gap") if f.status in OPEN]
    if gaps:
        out.append(_risk(f"{len(gaps)} IND readiness gap(s) open", Severity.MEDIUM, gaps))
    for f in group("sae_recon"):
        if (f.number or 0) > 0:
            out.append(
                _risk(f"{f.number:g} SAE reconciliation mismatch(es) on {f.value}",
                      Severity.HIGH, [f])
            )  # fmt: skip
    signals = [f for f in group("priority_signal") if f.status in OPEN]
    if signals:
        out.append(
            _risk(f"{len(signals)} priority safety signal(s) not yet reviewed",
                  Severity.MEDIUM, signals)
        )  # fmt: skip
    out.sort(key=by_severity_then_recency)
    return out


def _fact(f: Fact) -> OutlookFact:
    return OutlookFact(
        key=f.key,
        group=f.group,
        label=f.label,
        value=f.value,
        status=f.status,
        seq=f.seq,
        static=f.static,
    )


def changes(now: Sequence[OutlookFact], before: Sequence[OutlookFact] | None) -> list[str]:
    if before is None:
        return ["First outlook: nothing to compare with."]
    old = {f.key: f for f in before}
    new = {f.key: f for f in now}
    out = []
    for key in sorted(new.keys() - old.keys()):
        out.append(f"New: {new[key].label}: {new[key].value or new[key].status}")
    for key in sorted(new.keys() & old.keys()):
        a, b = old[key], new[key]
        if (a.value, a.status) != (b.value, b.status):
            out.append(
                f"Changed: {b.label}: {a.value or a.status} ({a.status}) to "
                f"{b.value or b.status} ({b.status})"
            )
    for key in sorted(old.keys() - new.keys()):
        out.append(f"Gone: {old[key].label}: {old[key].value or old[key].status}")
    return out or ["No change since the previous outlook."]


def outlook(
    asset_id: str,
    facts: Sequence[Fact],
    before: Sequence[OutlookFact] | None,
    static_text: str = "",
) -> AssetOutlook:
    mine = [f for f in facts if f.asset_id == asset_id]
    open_rows = [f for f in mine if f.group == OPEN_FINDING]
    shown = [_fact(f) for f in mine if f.group != OPEN_FINDING]
    for severity in ("high", "medium", "low"):
        rows = [f for f in open_rows if f.severity == severity]
        if rows:
            shown.append(
                OutlookFact(
                    key=f"open:{severity}",
                    group=OPEN_COUNT,
                    label=f"Open {severity} findings",
                    value=str(len(rows)),
                    status="open",
                    seq=max(f.seq for f in rows if f.seq is not None),
                )
            )
    if before is not None:
        before = [f for f in before if f.group != OPEN_FINDING]
    is_static = bool(mine) and all(f.static for f in mine)
    found = risks(mine)
    if is_static:
        status = "static"
        text = static_text or "Static fields only (config/portfolio.toml)"
    elif any(r.severity is Severity.HIGH for r in found):
        status, text = "at_risk", "At risk"
    elif found:
        status, text = "attention", "Needs attention"
    else:
        status, text = "on_track", "On track"
    return AssetOutlook(
        asset_id=asset_id,
        status=status,  # type: ignore[arg-type]
        static=is_static,
        status_text=text,
        facts=tuple(shown),
        risks=tuple(found[:TOP_RISKS]),
        more_risks=max(0, len(found) - TOP_RISKS),
        open_findings=len(open_rows),
        changes=tuple(changes(shown, before)),
    )


def for_display(recorded: PortfolioOutlook) -> PortfolioOutlook:
    """A recorded outlook as a person reads it: risks by severity then recency (at most
    TOP_RISKS), open findings counted rather than listed (1.0.0 listed each one), doses at
    3 significant figures. The ledger event keeps everything as recorded."""
    assets = []
    for a in recorded.assets:
        listed = [f for f in a.facts if f.group == OPEN_FINDING]
        facts = [
            f.model_copy(update={"value": display_doses(f.value), "label": display_doses(f.label)})
            for f in a.facts
            if f.group != OPEN_FINDING
        ]
        if listed:
            facts.append(
                OutlookFact(
                    key="open:all",
                    group=OPEN_COUNT,
                    label="Open findings",
                    value=str(len(listed)),
                    status="open",
                    seq=max(f.seq for f in listed if f.seq is not None),
                )
            )
        ranked = sorted(a.risks, key=by_severity_then_recency)
        assets.append(
            a.model_copy(
                update={
                    "facts": tuple(facts),
                    "risks": tuple(
                        r.model_copy(update={"text": display_doses(r.text)})
                        for r in ranked[:TOP_RISKS]
                    ),
                    "more_risks": a.more_risks + max(0, len(ranked) - TOP_RISKS),
                    "open_findings": a.open_findings or len(listed),
                    "changes": tuple(
                        display_doses(c)
                        for c in a.changes
                        if not (listed and c.startswith("New: ") and _names_open(c, listed))
                    ),
                }
            )
        )
    return recorded.model_copy(update={"assets": tuple(assets)})


def _names_open(change: str, listed: Sequence[OutlookFact]) -> bool:
    return any(change.startswith(f"New: {f.label}:") for f in listed)
