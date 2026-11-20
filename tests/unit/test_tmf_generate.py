"""The synthetic eTMF index and the TMF faults: determinism, the clean rules, ground truth."""

from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import polars as pl
import pytest

from meridian.domain import tmf as t
from meridian.faults.catalog import FaultError
from meridian.faults.inject import GroundTruth
from meridian.faults.reader import FullCut
from meridian.faults.tmf import (
    FAULT_TYPES,
    SET_SIZES,
    TmfFault,
    TmfScenario,
    expected,
    plant,
    scenario_set,
)
from meridian.platform.tmf_reference import load_tmf_reference
from meridian.tmf.generate import HARMLESS, generate, snapshot_seed
from meridian.tmf.milestones import parse_day, site_milestones, trial_first_subject
from tests.tmf_support import tmf_base as base_cut


def _truth(sc: TmfScenario, base: FullCut) -> GroundTruth:
    return GroundTruth(
        cut_id=sc.cut_id,
        scenario_path="x",
        scenario_sha256="0" * 64,
        seed=sc.seed,
        base_cut="DC-BASE",
        base_manifest_sha256="0" * 64,
        faults=tuple(plant(sc, base, REF).records),
    )


REF = load_tmf_reference()


def _dm() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "USUBJID": ["201-1001-001", "201-1001-002", "201-1001-003", "201-1002-001"],
            "SITEID": ["1001", "1001", "1001", "1002"],
            "RFICDTC": ["", "", "", ""],
            "RFSTDTC": ["2013-03-01", "2013-02-10", "", "2013-06-01T10:00"],
            "RFENDTC": ["2013-09-01", "2014-01-05", "", "2013-12-31"],
        }
    )


def test_milestones_come_from_the_configured_dm_fields() -> None:
    m = site_milestones(_dm())
    assert m["1001"].subjects == 3
    assert m["1001"].first_subject == date(2013, 2, 10)  # earliest RFSTDTC, blanks left out
    assert m["1001"].last_subject == date(2014, 1, 5)
    assert m["1002"].first_subject == date(2013, 6, 1)  # the date part of a date-time
    assert trial_first_subject(m) == date(2013, 2, 10)
    # RFICDTC is empty in the pilot: no site gets a first subject from it.
    assert all(v.first_subject is None for v in site_milestones(_dm(), "RFICDTC").values())
    assert parse_day("20/02/2013") is None
    assert parse_day("2013-02-30") is None


def test_the_index_is_deterministic_per_cut() -> None:
    a = generate(_dm(), REF, snapshot_seed("DC-X"))
    b = generate(_dm(), REF, snapshot_seed("DC-X"))
    c = generate(_dm(), REF, snapshot_seed("DC-Y"))
    assert a.frames()[t.ETMF_INDEX].equals(b.frames()[t.ETMF_INDEX])
    assert a.harmless == b.harmless
    assert not a.frames()[t.ETMF_INDEX].equals(c.frames()[t.ETMF_INDEX])


def test_the_clean_index_follows_every_rule() -> None:
    ix = generate(_dm(), REF, snapshot_seed("DC-X"))
    by_name = REF.by_name()
    firsts = {s: m.first_subject for s, m in site_milestones(_dm()).items()}
    for d in ix.documents:
        a = by_name[t.norm_name(d[t.ARTIFACT])]
        day, filed = date.fromisoformat(d[t.DOCUMENT_DATE]), date.fromisoformat(d[t.FILED_DATE])
        assert d[t.LEVEL] == a.level
        assert d[t.DOC_SITE] == d[t.SITE]
        assert day <= filed <= ix.export_date
        superseded = d[t.DOCUMENT_ID] in ix.harmless["superseded_late"]
        assert superseded or (filed - day).days <= a.file_within_days
        if a.dated_before and d[t.VERSION] == "1":
            first = firsts[d[t.SITE]]
            assert first is not None
            assert day <= first
    # Every visit has one report dated within the match window.
    for v in ix.visits:
        report = REF.for_visit(v.visit_type)
        assert report is not None
        near = [
            d
            for d in ix.documents
            if d[t.SITE] == v.site
            and t.norm_name(d[t.ARTIFACT]) == t.norm_name(report.name)
            and abs((date.fromisoformat(d[t.DOCUMENT_DATE]) - v.day).days)
            <= REF.rules.visit_match_days
        ]
        assert len(near) == 1
    # Routine visits are 6 to 10 weeks apart.
    routine = sorted(v.day for v in ix.visits if v.site == "1001" and v.visit_type == "routine")
    assert all(timedelta(weeks=6) <= b - a <= timedelta(weeks=10) for a, b in pairwise(routine))
    assert set(ix.harmless) == set(HARMLESS)


def test_the_index_has_every_harmless_variation_on_the_base_cut(tmp_path: Path) -> None:
    base = base_cut(tmp_path)
    ix = generate(base.blinded["dm"], REF, snapshot_seed("DC-BASE"))
    assert all(ix.harmless[h] for h in HARMLESS), ix.harmless


@pytest.mark.parametrize("fault", FAULT_TYPES)
def test_each_fault_is_planted_with_its_ground_truth(tmp_path: Path, fault: str) -> None:
    base = base_cut(tmp_path)
    for seed in range(1, 30):
        scenario = TmfScenario(
            set_name="dev",
            index=1,
            seed=seed,
            base_cut="DC-BASE",
            faults=(TmfFault(type=fault, params={}),),
        )
        try:
            planted = plant(scenario, base, REF)
        except FaultError:  # some seeds cannot place a fault; try the next
            continue
        checks = {r.kri for r in planted.records}
        assert fault in checks
        if fault == t.MISFILED:
            assert checks == {t.MISFILED, t.MISSING_ARTIFACT}
            doc = next(r.target for r in planted.records if r.kri == t.MISFILED)
            row = next(d for d in planted.index.documents if d[t.DOCUMENT_ID] == doc)
            assert row[t.SITE] != row[t.DOC_SITE]
        return
    pytest.fail(f"no seed could plant {fault}")


def test_the_sets_have_opaque_distinct_cut_ids_and_use_every_fault(tmp_path: Path) -> None:
    base = base_cut(tmp_path)
    dev = scenario_set("dev", base, REF)
    clean = scenario_set("clean", base, REF)
    assert len(dev) == SET_SIZES["dev"]
    assert len(clean) == SET_SIZES["clean"]
    assert all(not sc.faults for sc in clean)
    assert all(1 <= len(sc.faults) <= 3 for sc in dev)
    ids = [sc.cut_id for sc in dev + clean]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("DC-") and len(i) == 11 for i in ids)
    assert {f.type for sc in dev for f in sc.faults} == set(FAULT_TYPES)
    # The label is readable; the cut id says nothing about the scenario.
    assert dev[0].label == "tmf_dev_01"
    truth = {(r.kri, r.target) for sc in dev for r in plant(sc, base, REF).records}
    assert truth
    assert all(check in t.CHECKS for check, _ in truth)
    assert expected(_truth(dev[0], base)) == {
        (r.kri, r.target) for r in plant(dev[0], base, REF).records
    }
