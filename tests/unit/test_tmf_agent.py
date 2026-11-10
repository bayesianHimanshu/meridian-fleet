"""quality.tmf_completeness on generated indexes: clean is silent, every planted fault is
found under its check, bad records become data-quality findings, nothing names a subject."""

import re
from datetime import date
from pathlib import Path

import polars as pl
import pytest
import structlog

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.quality.tmf_completeness import (
    AGENT_ID,
    TmfCompleteness,
    assess,
    match_reports,
    readable,
)
from meridian.domain import tmf as t
from meridian.domain.enums import Role, Severity
from meridian.domain.events import DataCut, Event
from meridian.faults.catalog import FaultError
from meridian.faults.reader import FullCut
from meridian.faults.tmf import FAULT_TYPES, TmfFault, TmfScenario, plant
from meridian.platform.agent_settings import TmfCompletenessConfig
from meridian.platform.clock import FixedClock
from meridian.tmf.generate import Index, generate, snapshot_seed
from tests.agent_support import trigger
from tests.rbqm_support import NOW, STUDY
from tests.tmf_support import tmf_base

CFG = TmfCompletenessConfig()
CUT = "DC-TMF"


class Reader:
    study_id = STUDY
    cut_id = CUT

    def __init__(self, tables: dict[str, pl.DataFrame]) -> None:
        self._t = tables

    def table(self, name: str) -> pl.DataFrame:
        from meridian.data.study_store import MissingTableError

        if name not in self._t:
            raise MissingTableError(name)
        return self._t[name]

    def tables(self) -> list[str]:
        return sorted(self._t)


def _reader(dm: pl.DataFrame, index: Index) -> Reader:
    return Reader({"dm": dm, **index.frames()})


def run(reader: Reader, cfg: TmfCompletenessConfig = CFG) -> list[FindingDraft]:
    ctx = AgentContext(study=reader, clock=FixedClock(NOW), config=cfg, log=structlog.get_logger())
    event: Event = trigger(CUT)
    assert isinstance(event.payload, DataCut)
    return list(TmfCompleteness().run(event, ctx))


def checks(drafts: list[FindingDraft]) -> set[tuple[str, str]]:
    out = set()
    for d in drafts:
        name, _, record = d.dedup_key.split("|")[5].partition(":")
        out.add((name, record))
    return out


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> FullCut:
    return tmf_base(tmp_path_factory.mktemp("tmf"))


def test_a_clean_index_reconciles_to_zero(base: FullCut) -> None:
    dm = base.blinded["dm"]
    for cut in ("DC-A", "DC-B", "DC-C", "DC-D"):
        index = generate(dm, CFG.reference, snapshot_seed(cut))
        assert run(_reader(dm, index)) == []
        a = assess(_reader(dm, index), CFG)
        assert a.percent(None) == (100.0, 100.0)
        assert all(c.status in ("filed", "not_expected") for c in a.cells)


def test_every_harmless_variation_is_present_and_none_is_flagged(base: FullCut) -> None:
    """Over several seeds every variation occurs, and no finding names any such document."""
    dm = base.blinded["dm"]
    seen: dict[str, int] = {}
    for n in range(8):
        index = generate(dm, CFG.reference, snapshot_seed(f"DC-H{n}"))
        for kind, ids in index.harmless.items():
            seen[kind] = seen.get(kind, 0) + len(ids)
        assert run(_reader(dm, index)) == []
    assert all(v > 0 for v in seen.values()), seen


@pytest.mark.parametrize("fault", FAULT_TYPES)
def test_each_fault_is_flagged_with_its_check(base: FullCut, fault: str) -> None:
    dm = base.blinded["dm"]
    hits = 0
    for seed in range(1, 40):
        sc = TmfScenario(
            set_name="dev",
            index=1,
            seed=seed,
            base_cut="DC-BASE",
            faults=(TmfFault(type=fault, params={}),),
        )
        try:
            planted = plant(sc, base, CFG.reference)
        except FaultError:
            continue
        drafts = run(_reader(dm, planted.index))
        assert checks(drafts) == {(r.kri, r.target) for r in planted.records}
        hits += 1
        if hits == 3:
            return
    pytest.fail(f"{fault} planted fewer than 3 times")


def test_severity_follows_the_rule(base: FullCut) -> None:
    dm = base.blinded["dm"]
    sc = TmfScenario(
        set_name="dev",
        index=1,
        seed=3,
        base_cut="DC-BASE",
        faults=(
            TmfFault(type=t.APPROVAL_AFTER_FIRST_SUBJECT, params={}),
            TmfFault(type=t.MONITORING_REPORT_MISSING, params={}),
            TmfFault(type=t.QC_OVERDUE, params={}),
        ),
    )
    drafts = run(_reader(dm, plant(sc, base, CFG.reference).index))
    by_check = {d.dedup_key.split("|")[5].split(":")[0]: d.severity for d in drafts}
    assert by_check[t.APPROVAL_AFTER_FIRST_SUBJECT] is Severity.HIGH
    assert by_check[t.MONITORING_REPORT_MISSING] is Severity.HIGH
    assert by_check[t.QC_OVERDUE] is Severity.MEDIUM


def _rows(index: Index) -> list[dict[str, str]]:
    return [dict(d) for d in index.documents]


def test_bad_records_become_data_quality_findings_and_never_fail_the_cut(base: FullCut) -> None:
    dm = base.blinded["dm"]
    index = generate(dm, CFG.reference, snapshot_seed("DC-Q"))
    docs = sorted(_rows(index), key=lambda d: d[t.DOCUMENT_ID])
    trial = [d for d in docs if d[t.LEVEL] == "trial" and t.norm_name(d[t.ARTIFACT]) == "PROTOCOL"]
    site_doc = next(d for d in docs if t.norm_name(d[t.ARTIFACT]) == "SITE SIGNATURE SHEET")
    trial[0][t.DOCUMENT_DATE] = "not a date"  # excluded: Protocol is now missing
    site_doc[t.FILED_DATE] = "13/02/2013"  # still counts as filed
    docs.append({**docs[0], t.DOCUMENT_ID: "", t.ARTIFACT: "Protocol"})  # no id
    docs.append({**docs[1], t.DOCUMENT_ID: "TMF-NEW", t.ARTIFACT: "Investigator Statement"})
    index.documents = docs
    found = checks(run(_reader(dm, index)))
    assert (t.DATA_QUALITY, f"{trial[0][t.DOCUMENT_ID]}#DOCUMENT_DATE") in found
    assert (t.DATA_QUALITY, f"{site_doc[t.DOCUMENT_ID]}#FILED_DATE") in found
    assert (t.DATA_QUALITY, "row-1#DOCUMENT_ID") in found
    assert (t.DATA_QUALITY, "TMF-NEW#ARTIFACT") in found
    assert (t.MISSING_ARTIFACT, "TRIAL/protocol") in found
    site = site_doc[t.SITE]
    assert (t.MISSING_ARTIFACT, f"{site}/site_signature_sheet") not in found
    assert len(found) == 5
    # A whole table with the wrong columns, or missing: still no crash.
    empty = Reader({"dm": dm, t.ETMF_INDEX: pl.DataFrame({"X": ["1"]})})
    drafts = run(empty)
    assert (t.DATA_QUALITY, "export#EXPORT_DATE") in checks(drafts)


def test_no_finding_names_a_subject_and_visibility_comes_from_config(base: FullCut) -> None:
    dm = base.blinded["dm"]
    sc = TmfScenario(
        set_name="dev",
        index=1,
        seed=5,
        base_cut="DC-BASE",
        faults=tuple(TmfFault(type=f, params={}) for f in FAULT_TYPES[:3]),
    )
    drafts = run(_reader(dm, plant(sc, base, CFG.reference).index))
    assert drafts
    subjects = set(dm["USUBJID"].to_list())
    for d in drafts:
        text = d.model_dump_json()
        assert "USUBJID" not in text
        assert not any(s in text for s in subjects)
        assert set(d.visible_to or ()) == {
            Role.CLINICAL_OPS_LEAD,
            Role.MEDICAL_MONITOR,
            Role.QA_AUDITOR,
            Role.PORTFOLIO_LEAD,
        }
        # Dates are readable in every display string, never ISO.
        for e in d.evidence:
            assert not re.search(r"\d{4}-\d{2}-\d{2}", e.display or "")
    narrow = TmfCompletenessConfig(visible_to=(Role.QA_AUDITOR,))
    assert all(
        d.visible_to == (Role.QA_AUDITOR,)
        for d in run(_reader(dm, plant(sc, base, CFG.reference).index), narrow)
    )
    assert AGENT_ID == "quality.tmf_completeness"


def test_reports_match_visits_nearest_first() -> None:
    from meridian.agents.quality.tmf_completeness import Doc, Visit

    art = CFG.reference.by_key()["monitoring_visit_report"]
    v1 = Visit("V1", "1", "routine", date(2013, 6, 10))
    v2 = Visit("V2", "1", "routine", date(2013, 8, 5))

    def doc(i: str, d: date) -> Doc:
        return Doc(i, art, "1", "1", 1, d, d, "passed")

    both_on_v2 = [doc("A", date(2013, 8, 5)), doc("B", date(2013, 8, 5))]
    assert {k: v.doc_id for k, v in match_reports([v1, v2], both_on_v2, 3).items()} == {"V2": "A"}
    assert match_reports([v1], [doc("C", date(2013, 6, 14))], 3) == {}
    assert readable(date(2013, 1, 20)) == "20 Jan 2013"


def test_the_agent_is_registered_and_enabled() -> None:
    from meridian.platform.config import Settings

    registered = {
        r.agent.agent_id: r
        for r in __import__("meridian.agents.catalog", fromlist=["build_registry"]).build_registry(
            Settings().agents
        )
    }
    assert registered[AGENT_ID].enabled
    assert registered[AGENT_ID].agent.zone.value == "quality"


def test_milestones_use_only_dm_dates(tmp_path: Path) -> None:
    dm = pl.DataFrame(
        {
            "USUBJID": ["201-1-1", "201-1-2"],
            "SITEID": ["1", "1"],
            "RFSTDTC": ["2013-05-06", ""],
            "RFENDTC": ["2013-09-01", ""],
        }
    )
    index = generate(dm, CFG.reference, snapshot_seed("DC-M"))
    a = assess(_reader(dm, index), CFG)
    assert a.sites == ["1"]
    assert run(_reader(dm, index)) == []
