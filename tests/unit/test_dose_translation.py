"""preclinical.dose_translation (phase 5 step 2): the arithmetic against the FDA 2005
guidance's own worked example and a hand calculation, the conversion factor read from
config with its citation, and the agent's paths: no decision, not established, the basis
it chooses, an uncited species, and exposure shown as information only."""

import uuid
from pathlib import Path

import polars as pl
import pytest
import structlog

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.preclinical.dose_translation import (
    DoseTranslation,
    StagedDecision,
    basis_of,
    current_decisions,
)
from meridian.data.study_store import LayeredStudyReader, NoaelStageReader, StudyReader
from meridian.domain.enums import FindingKind, Visibility
from meridian.domain.events import Event, NoaelDecision
from meridian.platform.agent_settings import DoseTranslationConfig
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.preclinical.dose import NoCitedFactorError, per_kg, working
from meridian.preclinical.inputs import write_noael_extract
from meridian.preclinical.synth import STUDY, StudySpec
from tests.support import T0
from tests.tox_support import CUT, write_study

RATIONALE = "x" * 200


def cfg() -> DoseTranslationConfig:
    return Settings().agents.dose_translation


def test_the_rat_factor_comes_from_config_with_its_citation() -> None:
    rat = cfg().conversions["RAT"]
    assert (rat.km, rat.divide_by, rat.multiply_by) == (6.0, 6.2, 0.16)
    assert "section V.A, Table 1" in rat.citation
    assert cfg().safety_factor == 10.0
    assert "section VII" in cfg().safety_factor_citation
    assert "Assumes 60 kg human" in cfg().human_weight_citation


def test_the_guidance_worked_example_appendix_d() -> None:
    # Appendix D, Example 3 (division method): 50 mg/kg in rats / 6.2 = 8 mg/kg;
    # 15 mg/kg in dogs / 1.8 = 8 mg/kg (the guidance rounds to whole mg/kg).
    assert round(working(50, "mg/kg", "RAT", cfg()).hed_mg_per_kg) == 8
    assert round(working(15, "mg/kg", "DOG", cfg()).hed_mg_per_kg) == 8
    # Example 2, in two steps through km: (15 x 20) / 37 = 8 mg/kg.
    dog = cfg().conversions["DOG"]
    assert round(15 * dog.km / cfg().human_km) == 8
    # The multiplication column agrees to the guidance's rounding.
    assert round(working(50, "mg/kg", "RAT", cfg()).hed_by_multiplier_mg_per_kg) == 8


def test_the_hand_calculation_for_this_study() -> None:
    # NOAEL 2 mg/kg/day in the rat: HED 2 / 6.2 = 0.32258 mg/kg; MRSD / 10 = 0.032258;
    # 60 kg adult: 1.9355 mg.
    w = working(2, "mg/kg", "RAT", cfg())
    assert w.hed_mg_per_kg == pytest.approx(0.3225806, rel=1e-6)
    assert w.mrsd_mg_per_kg == pytest.approx(0.03225806, rel=1e-6)
    assert w.starting_dose_mg == pytest.approx(1.9354839, rel=1e-6)


def test_no_factor_is_used_without_a_citation() -> None:
    with pytest.raises(NoCitedFactorError):
        working(2, "mg/kg", "HAMSTER", cfg())
    with pytest.raises(ValueError, match="only mg/kg"):
        per_kg(2, "mg/m2")
    assert per_kg(2, "mg/kg/day") == 2


def _d(seq: int, sex: str, noael: float | None, supersedes: str | None = None) -> StagedDecision:
    return StagedDecision(
        seq=seq,
        decision_id=f"NOAEL-{seq:012x}",
        study_id=STUDY,
        sex=sex,
        established=noael is not None,
        noael_dose=noael,
        loael_dose=None,
        dose_unit="mg/kg/day",
        decided_by="leela.nair",
        supersedes=supersedes,
        recorded_at="2026-11-19T00:00:00+00:00",
    )


def test_the_basis_is_combined_if_recorded_else_the_most_sensitive_sex() -> None:
    m, f = _d(1, "M", 30.0), _d(2, "F", 10.0)
    assert basis_of([m, f]) == f
    assert basis_of([m, _d(3, "F", None)]).sex == "F"  # type: ignore[union-attr]
    assert basis_of([m, f, _d(4, "combined", 30.0)]).sex == "combined"  # type: ignore[union-attr]
    assert basis_of([]) is None
    revised = _d(5, "F", 30.0, supersedes=f.decision_id)
    assert current_decisions([m, f, revised]) == [m, revised]


def _event(seq: int, decision: NoaelDecision) -> Event:
    return Event(
        seq=seq,
        event_id=uuid.UUID(int=seq),
        occurred_at=T0,
        recorded_at=T0,
        actor="leela.nair",
        study_id=decision.study_id,
        visibility=Visibility.BLINDED,
        payload=decision,
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def _decision(
    seq: int, sex: str, noael: float | None, loael: float | None, supersedes: str | None = None
) -> Event:
    return _event(
        seq,
        NoaelDecision(
            decision_id=f"NOAEL-{seq:012x}",
            study_id=STUDY,
            sex=sex,  # type: ignore[arg-type]
            established=noael is not None,
            noael_dose=noael,
            loael_dose=loael,
            dose_unit="mg/kg/day",
            finding_ids=("PRE-0001",),
            rationale=RATIONALE,
            decided_by="leela.nair",
            supersedes=supersedes,
        ),
    )


def _reader(
    tmp: Path, events: list[Event], extra: dict[str, pl.DataFrame] | None = None
) -> StudyReader:
    send = write_study(tmp / "send", StudySpec(seed=7))
    for name, frame in (extra or {}).items():
        frame.write_parquet(tmp / "send" / STUDY / CUT / "send" / f"{name}.parquet")
    root = write_noael_extract(events, events[-1], CUT, tmp / "noael")
    return LayeredStudyReader(
        send, NoaelStageReader.at_cut_dir(root, cut_id=CUT, study_id=send.study_id)
    )


def _run(
    reader: StudyReader, trigger: Event, config: DoseTranslationConfig | None = None
) -> list[FindingDraft]:
    ctx = AgentContext(
        study=reader, clock=FixedClock(T0), config=config or cfg(), log=structlog.get_logger()
    )
    return list(DoseTranslation().run(trigger, ctx))


def test_a_proposal_with_every_step_as_evidence(tmp_path: Path) -> None:
    events = [_decision(10, "M", 30.0, 100.0), _decision(11, "F", 10.0, 30.0)]
    (draft,) = _run(_reader(tmp_path, events), events[-1])
    assert draft.kind is FindingKind.DOSE_PROPOSAL
    assert draft.dedup_key.endswith(f"|{CUT}|dose_proposal:NOAEL-{11:012x}")
    assert draft.actor_under_review == "leela.nair"
    steps = {e.statistic: e for e in draft.evidence}
    assert steps["mrsd:noael_mg_per_kg"].value == 10.0  # the more sensitive sex
    assert steps["mrsd:hed_mg_per_kg"].value == pytest.approx(10 / 6.2)
    assert steps["mrsd:mrsd_mg_per_kg"].value == pytest.approx(10 / 6.2 / 10)
    assert steps["mrsd:starting_dose_mg"].value == pytest.approx(10 / 6.2 / 10 * 60)
    for key in ("mrsd:hed_mg_per_kg", "mrsd:mrsd_mg_per_kg", "mrsd:starting_dose_mg"):
        assert steps[key].role == "primary"
        assert "FDA 2005" in (steps[key].display or "")
    assert "Table 1: Rat" in (steps["mrsd:hed_mg_per_kg"].display or "")
    assert "= 1.61 / 10 = 0.161 mg/kg" in (steps["mrsd:mrsd_mg_per_kg"].display or "")
    assert "9.68 mg" in draft.title
    limits = [e.display for e in draft.evidence if e.statistic == "mrsd:limitation"]
    assert any("One species only" in (t or "") for t in limits)
    assert any("no MABEL comparison" in (t or "") for t in limits)
    used = [e for e in draft.evidence if e.statistic == "mrsd:safety_factor_used"]
    assert used[0].value == 10.0
    exposure = steps["mrsd:exposure_information_only"]
    assert "no PC or PP domain" in (exposure.display or "")
    assert "proposal, not a decision" in draft.summary


def test_no_decision_records_nothing(tmp_path: Path) -> None:
    events = [_decision(10, "M", 30.0, 100.0)]
    reader = _reader(tmp_path, events)
    other = _event(
        9,
        NoaelDecision.model_validate(
            {**events[0].payload.model_dump(), "decision_id": "NOAEL-00000000000a"}
        ),
    )
    assert _run(reader, other) == []  # its own decision is after the trigger: none current


def test_not_established_says_no_starting_dose_can_be_derived(tmp_path: Path) -> None:
    events = [_decision(10, "combined", None, 10.0)]
    (draft,) = _run(_reader(tmp_path, events), events[-1])
    assert draft.dedup_key.endswith(f"no_starting_dose:NOAEL-{10:012x}")
    assert draft.title.startswith("No starting dose can be derived")
    assert not any(e.statistic == "mrsd:starting_dose_mg" for e in draft.evidence)


def test_an_uncited_species_gets_no_starting_dose(tmp_path: Path) -> None:
    events = [_decision(10, "combined", 10.0, 30.0)]
    no_rat = cfg().model_copy(update={"conversions": {"DOG": cfg().conversions["DOG"]}})
    (draft,) = _run(_reader(tmp_path, events), events[-1], no_rat)
    assert "no cited conversion" in draft.title
    assert "RAT" in draft.summary


def test_exposure_at_the_noael_is_information_only(tmp_path: Path) -> None:
    events = [_decision(10, "combined", 10.0, 30.0)]
    send = write_study(tmp_path / "probe", StudySpec(seed=7))
    dm = send.table("dm")
    tx = send.table("tx")
    sets = tx.filter((pl.col("TXPARMCD") == "TRTDOS") & (pl.col("TXVAL") == "10"))["SETCD"]
    animals = dm.filter(pl.col("SETCD").is_in(sets.to_list()))["USUBJID"].to_list()[:4]
    pp = pl.DataFrame(
        {
            "USUBJID": animals * 2,
            "PPTESTCD": ["AUCLST"] * 4 + ["CMAX"] * 4,
            "PPSTRESN": [100.0, 120.0, 110.0, 90.0, 10.0, 12.0, 11.0, 9.0],
            "PPSTRESU": ["h*ng/mL"] * 4 + ["ng/mL"] * 4,
            "PPDY": [90] * 8,
        }
    )
    (draft,) = _run(_reader(tmp_path, events, {"pp": pp}), events[-1])
    exposure = next(e for e in draft.evidence if e.statistic == "mrsd:exposure_information_only")
    assert exposure.role == "supporting"
    assert "Information only, not used" in (exposure.display or "")
    assert "AUCLST day 90 105 h*ng/mL (n = 4)" in (exposure.display or "")
    assert "CMAX day 90 10.5 ng/mL" in (exposure.display or "")
    start = next(e for e in draft.evidence if e.statistic == "mrsd:starting_dose_mg")
    assert start.value == pytest.approx(10 / 6.2 / 10 * 60)  # unchanged by exposure
