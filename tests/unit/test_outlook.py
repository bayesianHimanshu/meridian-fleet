"""The outlook's rules (phase 5 step 3b; docs/stage-gate-notes.md) and the report codec."""

from meridian.domain.enums import FindingKind
from meridian.domain.events import GatePack, OutlookFact, PortfolioOutlook
from meridian.portfolio.facts import Fact
from meridian.portfolio.outlook import changes, outlook
from meridian.portfolio.reports import draft, payload

A = "MRD-101"


def test_status_follows_the_most_severe_risk() -> None:
    ok = [Fact(A, "noael", "noael:M", "NOAEL (M)", "2 mg/kg/day", "established", seq=1),
          Fact(A, "starting_dose", "dose", "Dose", "1.94 mg", "accepted", seq=2),
          Fact(A, "ind_readiness", "ind", "IND", "100%", "assessed", seq=3)]  # fmt: skip
    assert outlook(A, ok, None).status == "on_track"
    gap = [*ok, Fact(A, "ind_gap", "g1", "Gap", "", "open", "medium", 4, "PRE-1")]
    o = outlook(A, gap, None)
    assert (o.status, [r.seqs for r in o.risks]) == ("attention", [(4,)])
    high = [*gap, Fact(A, "open_finding", "f", "Necrosis", "", "escalated", "high", 5, "PRE-2")]
    o = outlook(A, high, None)
    assert o.status == "at_risk"
    assert o.risks[0].severity.value == "high"  # high before medium
    no_noael = [f for f in ok if f.group != "noael"]
    assert outlook(A, no_noael, None).risks[0].text == "No NOAEL decision is recorded"
    static = [Fact("MRD-110", "static", "static:status_text", "Status", "On track", "static",
                   static=True)]  # fmt: skip
    s = outlook("MRD-110", static, None, "On track")
    assert (s.status, s.static, s.status_text, s.risks) == ("static", True, "On track", ())


def test_changes_against_the_previous_outlook() -> None:
    def f(key: str, value: str, status: str = "open") -> OutlookFact:
        return OutlookFact(key=key, group="g", label=key, value=value, status=status, seq=1)

    assert changes([f("a", "1")], None) == ["First outlook: nothing to compare with."]
    assert changes([f("a", "1")], [f("a", "1")]) == ["No change since the previous outlook."]
    got = changes([f("a", "2", "accepted"), f("b", "x")], [f("a", "1"), f("c", "y")])
    assert got == ["New: b: x", "Changed: a: 1 (open) to 2 (accepted)", "Gone: c: y"]


def test_a_report_draft_round_trips_into_its_event() -> None:
    body: dict[str, object] = {"review_id": "REVIEW-20261120T000000Z",
                               "previous_outlook_seq": None, "assets": []}  # fmt: skip
    d = draft(FindingKind.PORTFOLIO_OUTLOOK, "k", "t", "s", body)
    event = payload(d, "portfolio.pipeline_intelligence", "1.0.0", 7)
    assert isinstance(event, PortfolioOutlook)
    assert (event.trigger_seq, event.dedup_key, event.summary) == (7, "k", "s")
    pack = draft(FindingKind.GATE_PACK, "k2", "t", "s", {
        "gate_request_id": "GATE-20261120T000000Z", "asset_id": A, "gate": "ind_submission",
        "gate_title": "IND submission", "criteria": [], "open_items": []})  # fmt: skip
    assert isinstance(payload(pack, "portfolio.stage_gate_pack", "1.0.0", 8), GatePack)


# ---- wrap-up: pipeline_intelligence 1.1.0 and the display rules


def _highs(n: int) -> list[Fact]:
    return [
        Fact(A, "open_finding", f"open:PRE-{i}", f"Death {i}", "", "open", "high", 100 + i,
             f"PRE-{i}")
        for i in range(n)
    ]  # fmt: skip


def test_at_most_five_risks_by_severity_then_recency_with_the_rest_counted() -> None:
    gap = Fact(A, "ind_gap", "g1", "Gap", "", "open", "medium", 999, "PRE-G")
    lows = [Fact(A, "open_finding", f"open:L{i}", f"Low {i}", "", "open", "low", 200 + i)
            for i in range(3)]  # fmt: skip
    o = outlook(A, [*_highs(7), gap, *lows], None)
    # High first, newest first within high (seq 106 down to 102); the medium gap at seq 999
    # is newer but less severe, so it is beyond the top five.
    assert [r.seqs for r in o.risks] == [(106,), (105,), (104,), (103,), (102,)]
    assert o.more_risks == 3  # two highs and the gap
    # Open findings are counted by severity, not listed one by one.
    open_facts = [f for f in o.facts if f.group == "open_findings"]
    assert [(f.label, f.value, f.seq) for f in open_facts] == [
        ("Open high findings", "7", 106),
        ("Open low findings", "3", 202),
    ]
    assert not [f for f in o.facts if f.group == "open_finding"]
    assert o.open_findings == 10


def test_a_previous_outlook_listing_each_open_finding_is_not_reported_as_gone() -> None:
    before = [OutlookFact(key=f"open:PRE-{i}", group="open_finding", label=f"Death {i}",
                          value="", status="open", seq=100 + i) for i in range(3)]  # fmt: skip
    o = outlook(A, _highs(3), before)
    assert o.changes == ("New: Open high findings: 3",)


def test_a_recorded_1_0_0_outlook_is_shown_with_the_same_rules_and_doses_at_3_sig_figs() -> None:
    from meridian.domain.enums import Severity
    from meridian.domain.events import AssetOutlook, OutlookRisk
    from meridian.portfolio.outlook import for_display

    facts = (
        OutlookFact(key="starting_dose", group="starting_dose", label="Starting dose",
                    value="1.93548 mg", status="accepted", seq=5),
        *[OutlookFact(key=f"open:PRE-{i}", group="open_finding", label=f"Death {i}",
                      value="", status="open", seq=10 + i) for i in range(4)],
    )  # fmt: skip
    risks = (
        OutlookRisk(text="Older high", severity=Severity.HIGH, seqs=(10,)),
        OutlookRisk(text="Gaps", severity=Severity.MEDIUM, seqs=(20,)),
        OutlookRisk(text="Newer high", severity=Severity.HIGH, seqs=(13,)),
    )
    recorded = PortfolioOutlook(
        agent_id="portfolio.pipeline_intelligence", agent_version="1.0.0", trigger_seq=1,
        review_id="REVIEW-20261120T000000Z", dedup_key="k", summary="s",
        previous_outlook_seq=None,
        assets=(AssetOutlook(asset_id=A, status="at_risk", static=False, status_text="At risk",
                             facts=facts, risks=risks, changes=("First outlook",)),),
    )  # fmt: skip
    (shown,) = for_display(recorded).assets
    assert [r.text for r in shown.risks] == ["Newer high", "Older high", "Gaps"]
    assert [(f.label, f.value) for f in shown.facts] == [
        ("Starting dose", "1.94 mg"),
        ("Open findings", "4"),
    ]
    assert shown.open_findings == 4
    # The recorded event is unchanged: full precision, every open finding.
    assert recorded.assets[0].facts[0].value == "1.93548 mg"
    assert len(recorded.assets[0].facts) == 5


def test_doses_are_displayed_at_3_significant_figures() -> None:
    from meridian.domain.display import display_doses

    assert display_doses("PRE-0232 (1.93548 mg) is accepted.") == "PRE-0232 (1.94 mg) is accepted."
    assert display_doses("HED 0.32258 mg/kg, MRSD 0.0322581 mg/kg/day") == (
        "HED 0.323 mg/kg, MRSD 0.0323 mg/kg/day"
    )
    # Whole numbers, other units and version strings are left alone.
    for text in ("NOAEL 2 mg/kg/day", "200 mg/kg", "PRR 5.7277 (97 reports)", "v1.0.0 mg"):
        assert display_doses(text) == text


def test_a_recorded_gate_pack_is_shown_with_doses_at_3_sig_figs() -> None:
    from meridian.domain.events import CriterionResult
    from meridian.portfolio.gates import pack_for_display

    recorded = "PRE-0232 (1.93548 mg) is accepted."
    c = CriterionResult(id="starting_dose_accepted", title="t", basis="regulation",
                        source=None, section=None, quote=None, evidence="e", status="met",
                        explanation=recorded, seqs=(19966,))  # fmt: skip
    pack = GatePack(agent_id="a", agent_version="1.0.0", trigger_seq=1,
                    gate_request_id="GATE-20261120T000000Z", dedup_key="k", asset_id=A,
                    gate="ind_submission", gate_title="IND", summary="s", criteria=(c,),
                    open_items=())  # fmt: skip
    assert pack_for_display(pack).criteria[0].explanation == "PRE-0232 (1.94 mg) is accepted."
    assert pack.criteria[0].explanation == "PRE-0232 (1.93548 mg) is accepted."
