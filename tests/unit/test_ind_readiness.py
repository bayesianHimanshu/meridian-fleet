"""preclinical.ind_readiness (phase 5 step 2): the inventory and requirement checks, the
real inventory's expected result as pre-registered, the agent's findings with their
citations, and generated faults for regression (docs/ind-readiness-eval.md)."""

import uuid
from datetime import date
from pathlib import Path

import polars as pl
import pytest
import structlog

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.preclinical.ind_readiness import IndReadiness
from meridian.data.study_store import InventoryStageReader
from meridian.domain.enums import FindingKind, Severity, Visibility
from meridian.domain.events import Event, InventoryLoaded
from meridian.platform.agent_settings import IndReadinessConfig
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.preclinical.ind_cases import (
    apply_changes,
    complete_rows,
    evaluate,
    make_fault,
    real_rows,
    to_studies,
)
from meridian.preclinical.inputs import write_inventory
from meridian.preclinical.inventory import INVENTORY_FILE
from meridian.preclinical.readiness import assess, required_weeks
from tests.support import T0

ROOT = Path(__file__).resolve().parents[2]
STRESS = ROOT / "tests" / "fixtures" / "ind_stress"
REAL_GAPS = [
    "glp_status_missing:safety_pharmacology_respiratory:MRD-101-SP-03",
    "report_not_ready:repeat_dose_nonrodent:MRD-101-TOX-02",
    "report_not_ready:toxicokinetics_nonrodent:MRD-101-TOX-02-TK",
    "required_study_missing:in_vitro_metabolism_protein_binding",
]


def cfg(**update: object) -> IndReadinessConfig:
    return Settings().agents.ind_readiness.model_copy(update=update)


def test_the_real_inventory_gives_the_preregistered_gaps() -> None:
    result = evaluate(real_rows(), cfg())
    assert result.findings == REAL_GAPS
    assert result.percent_met == 63.6


def test_table1_durations() -> None:
    reqs = {r.id: r for r in cfg().requirements.requirements}
    rodent, nonrodent = reqs["repeat_dose_rodent"], reqs["repeat_dose_nonrodent"]
    assert required_weeks(rodent, cfg(planned_clinical_duration_days=10)) == 2.0
    assert required_weeks(rodent, cfg(planned_clinical_duration_days=14)) == 2.0
    assert required_weeks(rodent, cfg(planned_clinical_duration_days=42)) == 6.0
    assert required_weeks(nonrodent, cfg(planned_clinical_duration_days=182)) == 26.0
    assert required_weeks(rodent, cfg(planned_clinical_duration_days=270)) == 26.0
    assert required_weeks(nonrodent, cfg(planned_clinical_duration_days=270)) == 39.0
    assert required_weeks(reqs["in_vitro_ikr"], cfg()) is None


def test_the_best_candidate_and_each_check() -> None:
    by_id = {s.requirement.id: s for s in assess(to_studies(real_rows()), cfg())}
    rodent = by_id["repeat_dose_rodent"]
    assert [c.study_id for c in rodent.candidates] == ["MRD-101-TOX-01", "MRD-101-DRF-01"]
    assert rodent.study is not None
    assert rodent.study.study_id == "MRD-101-TOX-01"
    assert rodent.met
    nonrodent = by_id["repeat_dose_nonrodent"]
    assert nonrodent.study is not None
    assert nonrodent.study.study_id == "MRD-101-TOX-02"  # the longer of two with one problem
    assert nonrodent.problems == ("report_not_ready",)
    # A QA audit planned on the submission date counts; one day later does not.
    rows = apply_changes(
        complete_rows(STRESS),
        [
            {
                "set": "MRD-101-SP-04",
                "fields": {"report_status": "draft", "qa_audit_date": "2027-04-01"},
            }
        ],
    )
    assert evaluate(rows, cfg()).findings == []
    rows = apply_changes(
        rows, [{"set": "MRD-101-SP-04", "fields": {"qa_audit_date": "2027-04-02"}}]
    )
    assert evaluate(rows, cfg()).findings == ["report_not_ready:in_vitro_ikr:MRD-101-SP-04"]
    # A recorded "N" is not a finding; an empty GLP status is.
    rows = apply_changes(complete_rows(STRESS), [{"set": "MRD-101-SP-01", "fields": {"glp": "N"}}])
    assert evaluate(rows, cfg()).findings == []


def test_a_single_dose_trial_leaves_the_chromosomal_test_out() -> None:
    rows = apply_changes(complete_rows(STRESS), [{"drop": "MRD-101-GT-02"}])
    assert evaluate(rows, cfg()).findings == ["required_study_missing:genotoxicity_chromosomal"]
    single = evaluate(rows, cfg(multiple_dose=False))
    assert single.findings == []
    assert single.percent_met == 100.0
    statuses = assess(to_studies(rows), cfg(multiple_dose=False))
    assert {s.requirement.id: s.status for s in statuses}["genotoxicity_chromosomal"] == (
        "not_applicable"
    )


def _agent_drafts(tmp_path: Path) -> tuple[str, list[FindingDraft]]:
    cut, path, sha = write_inventory(ROOT / INVENTORY_FILE, tmp_path, "MRD-101")
    reader = InventoryStageReader.at_cut_dir(path, cut_id=cut, study_id="MRD-101")
    trigger = Event(
        seq=40,
        event_id=uuid.UUID(int=40),
        occurred_at=T0,
        recorded_at=T0,
        actor="system.ingest",
        study_id="MRD-101",
        visibility=Visibility.BLINDED,
        payload=InventoryLoaded(asset_id="MRD-101", cut_id=cut, path=str(path), source_sha256=sha),
        prev_hash="0" * 64,
        hash="1" * 64,
    )
    ctx = AgentContext(study=reader, clock=FixedClock(T0), config=cfg(), log=structlog.get_logger())
    return cut, list(IndReadiness().run(trigger, ctx))


def test_the_agent_raises_one_finding_per_gap_with_its_citation(tmp_path: Path) -> None:
    cut, drafts = _agent_drafts(tmp_path)
    assert cut.startswith("INV-")
    assert sorted(d.dedup_key.split("|")[-1] for d in drafts) == REAL_GAPS
    for d in drafts:
        assert d.kind is FindingKind.IND_READINESS
        assert d.dedup_key.split("|")[2] == "MRD-101"
        cite = [e for e in d.evidence if e.statistic == "ind:requirement"]
        assert len(cite) == 1
        assert cite[0].display is not None
        assert "ICH" in cite[0].display
        assert "not a judgement that the IND can or cannot be submitted" in d.summary
    missing = next(d for d in drafts if "required_study_missing" in d.dedup_key)
    assert missing.severity is Severity.HIGH
    assert "ICH M3(R2) III (3)" in missing.summary
    late = next(d for d in drafts if "repeat_dose_nonrodent" in d.dedup_key)
    assert "after the planned IND submission on 2027-04-01" in late.summary
    assert "120 days" in late.summary


def test_the_inventory_cut_is_named_after_its_csv(tmp_path: Path) -> None:
    a = write_inventory(ROOT / INVENTORY_FILE, tmp_path, "MRD-101")
    b = write_inventory(ROOT / INVENTORY_FILE, tmp_path, "MRD-101")
    assert a == b
    frame = pl.read_parquet(a[1] / "inventory" / "studies.parquet")
    assert frame.height == 12
    assert set(frame.schema.values()) == {pl.String}


@pytest.mark.parametrize("seed", range(1, 41))
def test_generated_faults_raise_exactly_their_finding(seed: int) -> None:
    complete = complete_rows(STRESS)
    assert evaluate(complete, cfg()).findings == []
    fault = make_fault(seed, complete, cfg())
    assert evaluate(fault.rows, cfg()).findings == [fault.expected], fault.kind


def test_every_fault_kind_is_generated() -> None:
    complete = complete_rows(STRESS)
    kinds = {make_fault(s, complete, cfg()).kind for s in range(1, 41)}
    assert kinds == {"drop", "shorten", "delay_report", "blank_glp"}
    assert date(2027, 4, 1) == cfg().planned_submission_date
