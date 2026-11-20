"""portfolio.safety_signal_scout (phase 5 step 3a): PRR, ROR and chi-squared against a
hand-computed 2 x 2 table; the Evans 2001 rule at its boundaries; the fetch plan (count
queries only, the two allowed paths only, the request cap); the label comparison; and a
rerun on the recorded responses giving identical drafts."""

import json
import uuid
from pathlib import Path
from urllib.parse import parse_qs

import pytest
import structlog

from meridian.agents.portfolio.safety_signal_scout import AGENT_ID, analyse, responses_frame
from meridian.data.stage_layout import SIGNAL
from meridian.domain.enums import Visibility
from meridian.domain.events import Event, SignalScanRequested
from meridian.platform.agent_settings import SignalScoutConfig
from meridian.platform.config import Settings
from meridian.portfolio import openfda as o
from meridian.portfolio.disproportionality import SignalRule, Table, chi2_yates, prr, ror
from meridian.portfolio.document import SignalDocument
from meridian.sandbox.staging import stage_areas
from meridian.signal_worker import execute, http_get
from tests.support import T0

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "default.toml"


def cfg() -> SignalScoutConfig:
    return Settings().agents.safety_signal_scout


def test_prr_ror_and_chi2_against_a_hand_computed_table() -> None:
    # a = 20 with the drug and the term, b = 980 with the drug only, c = 200 with the term
    # only, d = 98,800 neither (N = 100,000). By hand: PRR = (20 / 1,000) / (200 / 99,000)
    # = 9.9; ROR = (20 x 98,800) / (980 x 200) = 10.0816; Yates chi-squared =
    # 100,000 x (1,780,000 - 50,000)^2 / (1,000 x 99,000 x 220 x 99,780) = 137.72.
    t = Table.from_counts(20, 1_000, 220, 100_000)
    assert (t.a, t.b, t.c, t.d) == (20, 980, 200, 98_800)
    p, r = prr(t), ror(t)
    assert p is not None
    assert r is not None
    assert p.value == pytest.approx(9.9)
    assert p.low == pytest.approx(6.2784, abs=1e-4)
    assert p.high == pytest.approx(15.6105, abs=1e-4)
    assert r.value == pytest.approx(10.081633, rel=1e-6)
    assert r.low == pytest.approx(6.3393, abs=1e-3)
    assert r.high == pytest.approx(16.0332, abs=1e-3)
    assert chi2_yates(t) == pytest.approx(137.718, abs=1e-3)
    with pytest.raises(ValueError, match="inconsistent"):
        Table.from_counts(30, 20, 220, 100_000)


def test_the_signal_rule_at_its_boundaries() -> None:
    rule = SignalRule()
    # PRR exactly 2 (10 / 100 against 4,995 / 99,900), chi-squared 4.25: a signal.
    at_two = Table(10, 90, 4995, 94905)
    assert prr(at_two).value == 2.0  # type: ignore[union-attr]
    assert chi2_yates(at_two) >= 4
    assert rule.is_signal(at_two)
    just_under = Table(10, 90, 4996, 94904)  # PRR a hair below 2
    assert prr(just_under).value < 2  # type: ignore[union-attr]
    assert not rule.is_signal(just_under)
    # a = 3 with chi-squared 3.95 (not) and 4.05 (yes), PRR above 2 in both.
    low = Table(3, 126, 591, 100_000 - 3 - 126 - 591)
    high = Table(3, 134, 549, 100_000 - 3 - 134 - 549)
    assert prr(low).value >= 2  # type: ignore[union-attr]
    assert prr(high).value >= 2  # type: ignore[union-attr]
    assert chi2_yates(low) < 4 <= chi2_yates(high)
    assert not rule.is_signal(low)
    assert rule.is_signal(high)
    # Two reports are never a signal, however disproportionate.
    assert not rule.is_signal(Table(2, 8, 1, 999_989))
    assert rule.is_signal(Table(3, 7, 1, 999_989))


# ---- a fake openFDA


DRUG_TERMS = {
    "NAUSEA": 50, "HYPERHIDROSIS": 40, "VOMITING": 30, "PSYCHOTIC DISORDER": 20,
    "OFF LABEL USE": 10, "COLD SWEAT": 4, "HEADACHE": 4, "RARE THING": 2,
}  # fmt: skip
TERM_TOTALS = {
    "NAUSEA": 20_000, "HYPERHIDROSIS": 5_000, "VOMITING": 9_000, "PSYCHOTIC DISORDER": 2_000,
    "OFF LABEL USE": 50_000, "COLD SWEAT": 200, "HEADACHE": 30_000,
}  # fmt: skip
PLAN = ("NAUSEA", "HYPERHIDROSIS", "VOMITING", "PSYCHOTIC DISORDER", "OFF LABEL USE",
        "COLD SWEAT", "HEADACHE")  # fmt: skip
LABEL_TEXT = (
    "6 ADVERSE REACTIONS Most common adverse reactions (incidence ≥ 5% and at least twice "
    "placebo) were nausea, dyspepsia, vomiting, and hypertension. ( 6.1 )"
)


def _body(results: object, date: str = "2026-10-28") -> str:
    return json.dumps({"meta": {"last_updated": date}, "results": results})


THROTTLED = json.dumps(
    {"error": {"code": "SERVER_ERROR", "message": "Check your request and try again",
               "details": "Too Many Requests"}}
)  # fmt: skip


class FakeOpenFda:
    """throttle: query substring -> how many times to answer it as openFDA's throttling
    (HTTP 500 "Too Many Requests") before answering."""

    def __init__(
        self,
        date: str = "2026-10-28",
        throttle: dict[str, int] | None = None,
        extra: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.date = date
        self.throttle = dict(throttle or {})
        extra = extra or {}
        self.terms = {**DRUG_TERMS, **{t: a for t, (a, _) in extra.items()}}
        self.totals = {**TERM_TOTALS, **{t: n for t, (_, n) in extra.items()}}

    def __call__(self, path: str, query: str) -> tuple[int, str]:
        self.calls.append((path, query))
        for key, left in self.throttle.items():
            if key in query and left > 0:
                self.throttle[key] = left - 1
                return 500, THROTTLED
        q = {k: v[0] for k, v in parse_qs(query).items()}
        if path == o.LABEL:
            return 200, _body([{"set_id": "s-1", "version": "6", "effective_time": "20260116",
                                "adverse_reactions": [LABEL_TEXT]}], self.date)  # fmt: skip
        assert path == o.EVENT
        search = q.get("search", "")
        if q["count"] == o.REACTION_FIELD:
            rows = [{"term": t, "count": n} for t, n in self.terms.items()]
            return 200, _body(rows, self.date)
        if search.startswith(cfg().product_field):
            return 200, _body([{"term": 1, "count": 150}, {"term": 2, "count": 50}], self.date)
        if not search:
            return 200, _body([{"term": 1, "count": 1_000_000}], self.date)
        term = search.split(":", 1)[1].strip('"')
        if term not in self.totals:
            return 404, json.dumps({"error": {"code": "NOT_FOUND", "message": "No matches found!"}})
        return 200, _body([{"term": 2, "count": self.totals[term]}], self.date)


def test_the_plan_is_count_queries_on_the_two_paths_only() -> None:
    fake = FakeOpenFda()
    sleeps: list[float] = []
    responses = o.fetch(fake, cfg(), sleep=sleeps.append)
    assert {p for p, _ in fake.calls} == {o.EVENT, o.LABEL}
    for path, query in fake.calls:
        if path == o.EVENT:
            assert "count" in parse_qs(query)  # count queries only on the event endpoint
            assert "api_key" not in query
    terms = [parse_qs(q)["search"][0] for p, q in fake.calls[4:]]
    assert terms == [f'{o.REACTION_FIELD}:"{t}"' for t in PLAN]
    assert len(responses) == 11
    assert sleeps == [cfg().request_interval_seconds] * 10
    capped = o.fetch(
        FakeOpenFda(), cfg().model_copy(update={"max_requests": 6}), sleep=lambda _: None
    )
    assert len(capped) == 6
    _, get = http_get(cfg())
    with pytest.raises(ValueError, match=r"refusing to request /drug/ndc\.json"):
        get("/drug/ndc.json", "limit=1")


def test_signals_and_the_label() -> None:
    responses = o.fetch(FakeOpenFda(), cfg(), sleep=lambda _: None)
    scan = analyse(responses, cfg())
    assert (scan.drug_reports, scan.all_reports, scan.data_date) == (200, 1_000_000, "2026-10-28")
    by_term = {t.term: t for t in scan.tested}
    assert set(by_term) == set(PLAN)  # a >= 3 only
    nausea = by_term["NAUSEA"]
    assert (nausea.table.a, nausea.table.b, nausea.table.c) == (50, 150, 19_950)
    assert nausea.signal
    assert nausea.listed is True
    assert by_term["VOMITING"].listed is True
    assert by_term["OFF LABEL USE"].listed is False
    assert not by_term["HEADACHE"].signal  # 4 of 200 against 30,000 of 1,000,000: PRR 0.67
    # 1.0.1's tiers: the first that applies.
    tiers = {t: r.tier for t, r in by_term.items()}
    assert tiers == {
        "NAUSEA": "listed", "VOMITING": "listed", "PSYCHOTIC DISORDER": "indication",
        "HYPERHIDROSIS": "priority", "COLD SWEAT": "weak", "OFF LABEL USE": "not_signal",
        "HEADACHE": "not_signal",
    }  # fmt: skip
    assert scan.label is not None
    assert scan.label.most_common == ("nausea", "dyspepsia", "vomiting", "hypertension")


def _trigger(reuses: int | None = None) -> Event:
    return Event(
        seq=77,
        event_id=uuid.UUID(int=77),
        occurred_at=T0,
        recorded_at=T0,
        actor="meridian.signals",
        study_id="MRD-201",
        visibility=Visibility.BLINDED,
        payload=SignalScanRequested(
            study_id="MRD-201",
            scan_id="SCAN-20261120T000000Z",
            requested_by="operator:test",
            reuses_responses_of=reuses,
        ),
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def test_a_rerun_on_the_recorded_responses_gives_identical_drafts(tmp_path: Path) -> None:
    log = structlog.get_logger()
    live = stage_areas({}, _trigger(), tmp_path / "live")
    code, text = execute(AGENT_ID, live / "meta" / "trigger.json", live, CONFIG, log, FakeOpenFda())
    assert code == 0
    assert text is not None
    doc = SignalDocument.loads(text)
    assert not doc.replayed
    assert len(doc.responses) == 11
    assert all(r.status in (200, 404) for r in doc.responses)
    signals = sorted(d.dedup_key.split("|")[-1] for d in doc.drafts)
    # Only the priority signal is a finding (1.0.1): nausea and vomiting are in the label,
    # psychotic disorder is an indication term, cold sweat has 4 reports.
    assert signals == ["reporting_signal:HYPERHIDROSIS"]
    for d in doc.drafts:
        assert "not an incidence and not evidence that the drug causes it" in d.summary
        assert sum(e.statistic == "signal:caveat" for e in d.evidence) == 5
        context = [e.display or "" for e in d.evidence if e.statistic == "signal:context"]
        assert len(context) == 2
        assert "muscarinic antagonist" in context[0]
        assert "transdermal xanomeline alone" in context[1]
        (tier,) = [e.display or "" for e in d.evidence if e.statistic == "signal:tier"]
        assert tier.startswith("Tier: priority")
    # The host keeps the responses; a replay stages them and fetches nothing.
    kept = tmp_path / "kept" / SIGNAL
    kept.mkdir(parents=True)
    records = [o.Response(**r.model_dump()) for r in doc.responses]
    responses_frame(records).write_parquet(kept / f"{o.RESPONSES_TABLE}.parquet")
    replay = stage_areas({SIGNAL: kept}, _trigger(), tmp_path / "replay")

    def no_network(path: str, query: str) -> tuple[int, str]:
        raise AssertionError("a replay fetched")

    code, again = execute(
        AGENT_ID, replay / "meta" / "trigger.json", replay, CONFIG, log, no_network
    )
    assert code == 0
    assert again is not None
    doc2 = SignalDocument.loads(again)
    assert doc2.replayed
    assert doc2.drafts == doc.drafts  # identical drafts on the same responses
    # Another day's data (a new data date) gives other drafts: the date is in the evidence.
    later = stage_areas({}, _trigger(), tmp_path / "later")
    _, other = execute(AGENT_ID, later / "meta" / "trigger.json", later, CONFIG, log,
                       FakeOpenFda("2026-11-25"))  # fmt: skip
    assert other is not None
    assert SignalDocument.loads(other).drafts != doc.drafts


@pytest.mark.parametrize(
    "term",
    ["NAUSEA", "HALLUCINATION, AUDITORY", "VIOLENCE-RELATED SYMPTOM", "PARKINSON'S DISEASE",
     "DRUG SCREEN POSITIVE", "Ä/é (x) 100%"],
)  # fmt: skip
def test_the_query_encoding_is_urlencode(term: str) -> None:
    from urllib.parse import urlencode

    query = o.term_total_query(cfg(), term)
    expected = urlencode([("search", f'{o.REACTION_FIELD}:"{term}"'), ("count", cfg().total_field)])
    assert query == expected


# ---- 1.0.1: retry, rerun on recorded responses, triage


def test_throttling_is_retried_after_each_backoff_and_then_given_up() -> None:
    fake = FakeOpenFda(throttle={"HYPERHIDROSIS": 2, "VOMITING": 9})
    sleeps: list[float] = []
    responses = o.fetch(fake, cfg(), sleep=sleeps.append)
    hyper = [r for r in responses if "HYPERHIDROSIS" in r.query]
    assert [r.status for r in hyper] == [500, 500, 200]
    vomiting = [r for r in responses if "VOMITING" in r.query]
    assert [r.status for r in vomiting] == [500] * 5  # four retries, then not answered
    assert 2.0 in sleeps
    assert 16.0 in sleeps
    assert sleeps.count(16.0) == 1  # only VOMITING reached the last backoff
    scan = analyse(responses, cfg())
    assert "VOMITING" in scan.not_answered
    assert "HYPERHIDROSIS" not in scan.not_answered
    assert o.throttled(o.Response.of(o.EVENT, "q", 429, ""))
    assert not o.throttled(o.Response.of(o.EVENT, "q", 500, '{"error": {"details": "x"}}'))
    assert not o.throttled(o.Response.of(o.EVENT, "q", 404, ""))


def test_retries_count_toward_the_request_cap() -> None:
    capped = cfg().model_copy(update={"max_requests": 6})
    out = o.fetch(FakeOpenFda(throttle={"NAUSEA": 9}), capped, sleep=lambda _: None)
    assert len(out) == 6


def test_a_rerun_asks_only_what_the_earlier_scan_lacks() -> None:
    first = o.fetch(FakeOpenFda(throttle={"COLD+SWEAT": 9}), cfg(), sleep=lambda _: None)
    assert "COLD SWEAT" in analyse(first, cfg()).not_answered
    fake = FakeOpenFda()
    merged = o.top_up(fake, cfg(), first, sleep=lambda _: None)
    assert len(fake.calls) == 1
    assert "COLD+SWEAT" in fake.calls[0][1]
    assert merged[: len(first)] == first
    assert analyse(merged, cfg()).not_answered == ()
    # Nothing missing: nothing asked.
    again = FakeOpenFda()
    assert o.top_up(again, cfg(), merged, sleep=lambda _: None) == merged
    assert again.calls == []
    # openFDA's data moved on: refused, a full scan is needed.
    with pytest.raises(o.DataDateChangedError, match="full scan"):
        o.top_up(FakeOpenFda("2026-11-25"), cfg(), first, sleep=lambda _: None)


def _tier(term: str, a: int, low: float, listed: bool = False) -> str:
    from meridian.agents.portfolio.safety_signal_scout import tier_of
    from meridian.portfolio.disproportionality import Ratio

    return tier_of(term, True, listed, a, Ratio(value=low * 2, low=low, high=low * 4), cfg())


def test_the_triage_at_its_boundaries() -> None:
    assert _tier("HYPERHIDROSIS", 10, 2.0001) == "priority"
    assert _tier("HYPERHIDROSIS", 10, 2.0) == "weak"  # above 2, not at 2
    assert _tier("HYPERHIDROSIS", 9, 50.0) == "weak"
    assert _tier("HYPERHIDROSIS", 100, 50.0, listed=True) == "listed"
    assert _tier("SCHIZOPHRENIA", 100, 50.0) == "indication"
    assert _tier("hallucination, auditory", 100, 50.0) == "indication"
    assert _tier("TREATMENT NONCOMPLIANCE", 100, 50.0) == "use"
    from meridian.agents.portfolio.safety_signal_scout import tier_of

    assert tier_of("HYPERHIDROSIS", False, False, 100, None, cfg()) == "not_signal"


def test_the_label_comparison_spellings_and_synonym() -> None:
    label = o.Label(
        set_id="s", version="6", effective_time="2026",
        text=" ".join(o.normalise("gastrointestinal reflux disease, oedema-free esophageal "
                                  "pain, diarrhea").split()),
        most_common=(),
    )  # fmt: skip
    c = cfg()
    assert o.listed("GASTROOESOPHAGEAL REFLUX DISEASE", label, c.label_spellings, c.label_synonyms)
    assert not o.listed("GASTROOESOPHAGEAL REFLUX DISEASE", label)  # 1.0.0's rule
    assert o.listed("DIARRHOEA", label, c.label_spellings, c.label_synonyms)
    assert o.listed("OESOPHAGEAL PAIN", label, c.label_spellings, c.label_synonyms)
    assert not o.listed("HICCUPS", label, c.label_spellings, c.label_synonyms)


def test_the_worker_reruns_on_staged_earlier_responses(tmp_path: Path) -> None:
    log = structlog.get_logger()
    first = o.fetch(FakeOpenFda(throttle={"HYPERHIDROSIS": 9}), cfg(), sleep=lambda _: None)
    area = tmp_path / "prior" / SIGNAL
    area.mkdir(parents=True)
    responses_frame(first).write_parquet(area / f"{o.PRIOR_TABLE}.parquet")
    stage = stage_areas({SIGNAL: area}, _trigger(reuses=5), tmp_path / "rerun")
    fake = FakeOpenFda()
    code, text = execute(AGENT_ID, stage / "meta" / "trigger.json", stage, CONFIG, log, fake)
    assert code == 0, text
    assert text is not None
    doc = SignalDocument.loads(text)
    assert not doc.replayed
    assert len(fake.calls) == 1  # only HYPERHIDROSIS, which the first scan never got
    assert len(doc.responses) == len(first) + 1
    assert [d.dedup_key.split("|")[-1] for d in doc.drafts] == ["reporting_signal:HYPERHIDROSIS"]
    # Staged earlier responses on a scan that reuses none: refused as a data error.
    wrong = stage_areas({SIGNAL: area}, _trigger(), tmp_path / "wrong")
    code, _ = execute(AGENT_ID, wrong / "meta" / "trigger.json", wrong, CONFIG, log, FakeOpenFda())
    assert code == 3
