"""Build data/narrator_eval/labelled.jsonl and docs/narrator-labels.md from the ledger.

    uv run python scripts/build_narrator_labels.py

Takes every accepted rbqm.narrator 1.0.0 narrative on DC-BASE, DC-RBQM-CORE and
DC-51448ab6, labels it with LABELS below (assistant-judged: read against the finding's
summary and evidence), and adds seeded mutations of every real_good narrative. Each
record holds the input as narrator 1.0.0 saw it and as 1.1.0 would (evidence described).
Deterministic: the same ledger and seed give the same file.
"""

from pathlib import Path

from meridian.agents.rbqm import narrator as v1_1
from meridian.agents.rbqm import narrator_v1_0 as v1_0
from meridian.agents.rbqm.narrator_eval import LabelledRecord, Narrative, mutations_of
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType
from meridian.domain.events import Event, FindingNarrative, FindingRaised
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import SystemClock
from meridian.platform.config import Settings

SEED = 20261107
OUT = Path("data/narrator_eval/labelled.jsonl")
DOC = Path("docs/narrator-labels.md")
CUTS = ("DC-BASE", "DC-RBQM-CORE", "DC-51448ab6")

# finding_id -> (origin, expected 1.1.0 reason codes, one line of justification).
# real_bad are the five known-bad narratives of step 3; EXCLUDED were judged neither
# clearly good nor one of those five, and are left out of the set.
REAL_BAD: dict[str, tuple[tuple[str, ...], str]] = {
    "RBQM-0001": (
        ("zero_p_value",),
        "Says 'adjusted p values of 0' (underflow), and presents zero shares near the 10% "
        "chance level as what the flag concerns.",
    ),
    "RBQM-0006": (
        ("misattribution",),
        "'The summary notes that 49% ... end in zero': the summary says no such thing; the "
        "figures come from the evidence.",
    ),
    "RBQM-0010": (
        (),
        "Presents a 15% zero share (near chance) as the terminal-digit pattern. No rule "
        "targets this framing; any rejection counts as recall, not as the expected reason.",
    ),
    "RBQM-0035": (
        ("unbacked_ratio",),
        "'The lag is therefore twice the limit': a ratio the finding does not state.",
    ),
    "RBQM-0037": (
        ("threshold_restated",),
        "'having reached 90% of the limit': the rate is 91.9% of it; 90% is where "
        "'approaching' starts.",
    ),
}
EXCLUDED: dict[str, str] = {
    "RBQM-0042": "Adds a definition of entry lag that the finding does not state; correct "
    "but not from the finding, so neither clearly good nor one of the five.",
}
GOOD: dict[str, str] = {
    "RBQM-0002": "Terminal-digit pattern at 1003 for all three parameters, adjusted p below "
    "0.01, medium severity: all as in the finding.",
    "RBQM-0003": "Pattern at 1004 with p far below 0.01; says the cause is not established, "
    "which is fair.",
    "RBQM-0004": "Pattern at 1005; 'the summary notes ... Bonferroni-adjusted over 78 "
    "tests' is true of the summary.",
    "RBQM-0005": "Pattern at 1008; zero shares 56%, 49%, 28% match 0.558, 0.486, 0.277 and "
    "are well above chance.",
    "RBQM-0007": "Pattern at 1010; 'about 40% of the blood pressure readings end in zero' "
    "matches 0.405 and 0.403.",
    "RBQM-0008": "Pattern at 1013; 91.6% zeros for SYSBP and DIABP matches 0.9164.",
    "RBQM-0009": "Pattern at 1014; 'the summary notes ... 78 tests' is true of the summary.",
    "RBQM-0011": "Pattern at 1016; 46% of systolic readings end in zero matches 0.461.",
    "RBQM-0012": "Pattern at 1017; '17% to 29%' spans 0.171 to 0.294.",
    "RBQM-0013": "Pattern at 1018; says the flag is about recorded digits, not clinical: fair.",
    "RBQM-0014": "1001 on DC-RBQM-CORE: digit patterns and, for systolic and pulse, "
    "unusual spread, as the summary lists.",
    "RBQM-0015": "Pattern at 1003; 'the summary lists terminal digit' is true.",
    "RBQM-0016": "Pattern at 1004; 'the summary says the difference is driven by terminal "
    "digit patterns' is a fair paraphrase of the listed checks.",
    "RBQM-0017": "Pattern at 1005, p far below 0.01, cause not claimed.",
    "RBQM-0018": "Pattern at 1008; zero shares 56%, 49%, 28% match the evidence.",
    "RBQM-0019": "Pattern at 1009; zero shares 49%, 39%, 20% match the evidence.",
    "RBQM-0020": "Pattern at 1010, cause not claimed.",
    "RBQM-0021": "Pattern at 1013; zero share about 0.92 matches 0.9164, reported without "
    "interpretation.",
    "RBQM-0022": "Pattern at 1014; 28% zero share for SYSBP matches 0.279; calls it "
    "statistically unusual, not an error.",
    "RBQM-0023": "Pattern at 1015; lists zero shares 15%, 13%, 15% neutrally, without "
    "calling them the anomaly.",
    "RBQM-0024": "Pattern at 1016; 46.1% of systolic readings end in zero matches 0.461.",
    "RBQM-0025": "Pattern at 1017; 17%, 28%, 29% match the evidence.",
    "RBQM-0026": "Pattern at 1018, 78 tests, medium severity: as in the finding.",
    "RBQM-0027": "1005 under-reporting: 27 events over 2075 subject-days against 70.2, p and "
    "adjusted p as in the summary.",
    "RBQM-0028": "1015 under-reporting: 15 over 1160 subject-days against 38.6, adjusted p "
    "1.68e-04.",
    "RBQM-0029": "1016 under-reporting: 86 against about 134, p values as in the summary.",
    "RBQM-0030": "1005 under-reporting on DC-RBQM-CORE: 27 against about 64.3.",
    "RBQM-0031": "1010 under-reporting: 42 against about 130 (129.8), p values as given.",
    "RBQM-0032": "1015 under-reporting: 15 against about 35.4; adjusted p 0.00123 is 1.23e-03.",
    "RBQM-0033": "1016 under-reporting: 86 against about 122, p values as given.",
    "RBQM-0034": "1016 deviations: 18 against about 5.1, adjusted p 9.03e-05.",
    "RBQM-0036": "1008 query aging: median 40.0 days for 43 queries, above 21, high severity.",
    "RBQM-0038": "Study-wide QTL breach: 170 of 254 (66.9%) above 61.7%.",
    "RBQM-0039": "1001 variance: smaller spread for SYSBP and PULSE, SD below 0.6 of the "
    "median site, digits only supporting.",
    "RBQM-0040": "1008 deviations: 10 against about 3.0, adjusted p 1.89e-03 over 13 sites.",
    "RBQM-0041": "1016 deviations: 18 against about 5.1, adjusted p 1.57e-06 below 0.01.",
    "RBQM-0043": "1008 query aging: as RBQM-0036.",
    "RBQM-0051": "Study-wide QTL breach: 170 of 254 (66.9%) above 61.7%.",
    "RBQM-0081": "1016 variance on holdout 2: less spread for SYSBP and pulse, SD below 0.6, "
    "high severity, digits supporting.",
    "RBQM-0082": "1008 deviations: as RBQM-0040.",
    "RBQM-0083": "1016 deviations: 18 against about 5.1, adjusted p 1.57e-06.",
    "RBQM-0084": "1004 entry lag: median 14.0 days over 325 visits against 7, high severity.",
    "RBQM-0085": "1008 query aging: median 40 days for 43 queries above 21, high severity.",
    "RBQM-0116": "1004 deviations on holdout 2: 17 over 3229 subject-days against about 3.7.",
    "RBQM-0117": "1008 entry lag on holdout 2: median 14.0 days over 325 visits against 7.",
    "RBQM-0118": "1001 query aging on holdout 2: median 40.0 days for 62 queries above 21.",
    "RBQM-0122": "Study-wide QTL breach on holdout 2: 162 of 254 (63.8%) above 61.7%.",
}


def main() -> None:
    s = Settings()
    ledger = SqliteLedger(
        path=Path(s.ledger_path), clock=SystemClock(), finding_id_prefixes=s.finding_id_prefixes
    )
    raised: dict[int, tuple[FindingRaised, str]] = {}
    for e in ledger.read(event_types=[EventType.FINDING_RAISED]):
        if isinstance(e, Event) and isinstance(e.payload, FindingRaised):
            raised[e.seq] = (e.payload, parse_dedup_key(e.payload.dedup_key).cut_id)
    narratives = []
    for e in ledger.read(event_types=[EventType.FINDING_NARRATIVE]):
        if isinstance(e, Event) and isinstance(e.payload, FindingNarrative):
            n = e.payload
            if n.agent_version == "1.0.0" and n.status == "accepted":
                narratives.append(n)
    ledger.close()

    labelled = set(REAL_BAD) | set(EXCLUDED) | set(GOOD)
    seen = {n.finding_id for n in narratives}
    if seen != labelled:
        raise SystemExit(f"labels and narratives differ: {sorted(seen ^ labelled)}")
    records: list[LabelledRecord] = []
    sites: set[str] = set()
    for n in sorted(narratives, key=lambda n: n.finding_id):
        if n.finding_id in EXCLUDED:
            continue
        finding_raised, cut = raised[n.finding_seq]
        if cut not in CUTS:
            continue
        finding = finding_raised.finding
        if finding.site_id:
            sites.add(finding.site_id)
        bad = n.finding_id in REAL_BAD
        reasons, note = REAL_BAD[n.finding_id] if bad else ((), GOOD[n.finding_id])
        assert n.headline is not None
        assert n.explanation is not None
        records.append(
            LabelledRecord(
                id=f"{n.finding_id}/{'real_bad' if bad else 'real_good'}",
                finding_id=n.finding_id,
                cut=cut,
                origin="real_bad" if bad else "real_good",
                label="bad" if bad else "good",
                expected_reasons=reasons,
                labelled_by="assistant",
                note=note,
                narrative=Narrative(headline=n.headline, explanation=n.explanation),
                input_1_0=v1_0.narrator_input(finding),
                input_1_1=v1_1.narrator_input(finding),
            )
        )
    good = [r for r in records if r.origin == "real_good"]
    records += mutations_of(good, SEED, sorted(sites))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8")
    _write_doc(records)
    counts = {o: sum(r.origin == o for r in records) for o in ("real_good", "real_bad", "mutated")}
    print(f"wrote {OUT}: {len(records)} records {counts}")


def _write_doc(records: list[LabelledRecord]) -> None:
    lines = [
        "# Narrator labels (assistant-judged)",
        "",
        "Every accepted rbqm.narrator 1.0.0 narrative on DC-BASE, DC-RBQM-CORE and holdout 2",
        "(DC-51448ab6), labelled by the assistant (Claude) after reading it against its",
        "finding's summary and evidence. These labels are a judgment, not ground truth: please",
        "spot-check them. Generated with the labelled set by",
        "`uv run python scripts/build_narrator_labels.py` (data/narrator_eval/labelled.jsonl).",
        "",
        f"real_bad: {len(REAL_BAD)}. real_good: {len(GOOD)}. Excluded: {len(EXCLUDED)}.",
        "",
        "## real_bad",
        "",
    ]
    for fid, (reasons, note) in sorted(REAL_BAD.items()):
        lines.append(f"* **{fid}** (expected: {', '.join(reasons) or 'no specific code'}). {note}")
    lines += ["", "## Excluded", ""]
    lines += [f"* **{fid}**. {note}" for fid, note in sorted(EXCLUDED.items())]
    lines += ["", "## real_good", ""]
    by_id = {r.finding_id: r for r in records if r.origin == "real_good"}
    for fid, note in sorted(GOOD.items()):
        lines.append(f"* **{fid}** ({by_id[fid].cut}). {note}")
    lines += [
        "",
        "## Mutations",
        "",
        "Each real_good narrative is mutated once per type where the type applies (seed "
        f"{SEED}), and every mutation is labelled bad: wrong_number (one number in the",
        "explanation moved to a nearby value that appears nowhere in the input), flip_direction",
        "(fewer/more, below/above, smaller/larger, lower/higher, little/much), swap_site (the",
        "site id replaced by another study site), unbacked_ratio (a ratio phrase inserted into",
        "the first sentence), supporting_headline (the headline replaced by a supporting",
        "item's display) and zero_p (a p value replaced by 0). The mutations and validator",
        "1.1.0 have the same author, so recall on them is an optimistic measure.",
        "",
    ]
    DOC.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
