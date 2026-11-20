"""preclinical.tox_study_monitor 1.0.0: the pre-registered rules on synthetic studies
(docs/preclinical-eval.md). Specs here are the tests' own (other seeds than the stress
set, which runs once at the end)."""

import json
import re
from collections import Counter
from pathlib import Path

import polars as pl
import pytest

from meridian.agents.base import AgentContext
from meridian.agents.preclinical.tox_study_monitor import AGENT_ID, VERSION, ToxStudyMonitor
from meridian.domain.dedup import dedup_prefix
from meridian.domain.enums import FindingKind, Role
from meridian.platform.clock import FixedClock
from meridian.preclinical.analysis import (
    DOSE_RELATED_CHANGE,
    PAIRWISE_CHANGE,
    _continuous,
    analyse,
    by_key,
    read_design,
)
from meridian.preclinical.synth import Edit, Effect, StudySpec, build
from tests.support import T0
from tests.tox_support import checks, config, run, send_trigger, write_study
from tests.worker_support import cut_trigger


def test_a_dose_related_rise_is_found_in_both_sexes(tmp_path: Path) -> None:
    spec = StudySpec(seed=31, effects=(Effect(endpoint="lb:ALT", factors=(1, 1.1, 1.6, 2.5)),))
    drafts = run(tmp_path, spec)
    assert checks(drafts) == [
        "dose_related_change:lb:ALT:F:main",
        "dose_related_change:lb:ALT:M:main",
    ]
    summary = by_key(analyse(build(spec), config()))["lb:ALT:M:main"]
    assert [g.dose for g in summary.groups if g.flagged] == [30.0, 100.0]
    assert summary.trend_p is not None
    assert summary.trend_p < 0.01


def test_an_effect_in_one_sex_is_reported_for_that_sex_only(tmp_path: Path) -> None:
    spec = StudySpec(
        seed=32, effects=(Effect(endpoint="lb:AST", sex="F", factors=(1, 1.3, 1.8, 2.6)),)
    )
    assert checks(run(tmp_path, spec)) == ["dose_related_change:lb:AST:F:main"]


def test_an_effect_only_at_the_mid_dose_is_a_change_without_a_trend(tmp_path: Path) -> None:
    spec = StudySpec(seed=33, effects=(Effect(endpoint="lb:GLUC", factors=(1, 1, 1.5, 1)),))
    drafts = run(tmp_path, spec)
    assert checks(drafts) == ["pairwise_change:lb:GLUC:F:main", "pairwise_change:lb:GLUC:M:main"]
    assert all("no trend with dose" in d.title for d in drafts)
    assert not any("Dose-related" in d.summary for d in drafts)


def test_a_single_animal_outlier_is_not_a_finding(tmp_path: Path) -> None:
    edit = Edit(kind="lb_factor", sex="M", group=3, animal=2, test="CREAT", value=2.6)
    assert checks(run(tmp_path, StudySpec(seed=34, edits=(edit,)))) == []


def test_the_magnitude_must_hold_without_the_most_extreme_animal() -> None:
    cfg = config()
    control = [1.00, 1.01, 0.99, 1.00, 1.02, 0.98, 1.00, 1.01, 0.99, 1.00]
    one_animal = [1.00, 1.01, 0.99, 1.00, 1.02, 0.98, 1.00, 1.01, 0.99, 6.00]
    whole_group = [1.40, 1.41, 1.39, 1.40, 1.42, 1.38, 1.40, 1.41, 1.39, 1.40]
    from meridian.preclinical.analysis import Endpoint

    ep = Endpoint("lb:CREAT", "CREAT", "continuous", "clinical_chemistry", "mg/dL")
    s = _continuous(ep, "M", "main", {0: control, 1: one_animal}, [0.0, 100.0], cfg, [])
    assert s is not None
    group = s.groups[1]
    assert group.pct is not None
    assert group.pct > 40
    assert group.pct_without_extreme is not None
    assert abs(group.pct_without_extreme) < 1
    assert not group.flagged
    s2 = _continuous(ep, "M", "main", {0: control, 1: whole_group}, [0.0, 100.0], cfg, [])
    assert s2 is not None
    assert s2.groups[1].flagged


def test_a_small_significant_change_below_the_magnitude_is_not_a_finding() -> None:
    cfg = config()
    from meridian.preclinical.analysis import Endpoint

    ep = Endpoint("lb:ALT", "ALT", "continuous", "clinical_chemistry", "U/L")
    control = [40.0 + 0.1 * i for i in range(10)]
    s = _continuous(
        ep, "M", "main", {0: control, 1: [x * 1.12 for x in control]}, [0.0, 1.0], cfg, []
    )
    assert s is not None
    assert s.groups[1].p is not None
    assert s.groups[1].p < 0.001
    assert s.check is None


def test_hypertrophy_rising_with_dose_carries_its_severity(tmp_path: Path) -> None:
    effect = Effect(endpoint="mi:LIVER:HYPERTROPHY", counts=(0, 1, 4, 9), severity="MINIMAL")
    drafts = run(tmp_path, StudySpec(seed=35, effects=(effect,)))
    assert checks(drafts) == [
        "dose_related_incidence:mi:LIVER:HYPERTROPHY:F:main",
        "dose_related_incidence:mi:LIVER:HYPERTROPHY:M:main",
    ]
    displays = [e.display for d in drafts for e in d.evidence if e.display]
    assert "100 mg/kg/day: 9 of 10; grade 1: 9, Fisher p < 0.001" in displays


def test_recovery_groups_are_analysed_apart_from_the_main_study(tmp_path: Path) -> None:
    spec = StudySpec(
        seed=36,
        recovery=True,
        effects=(Effect(endpoint="lb:ALT", phase="main", factors=(1, 1.2, 1.6, 2.5)),),
    )
    assert checks(run(tmp_path, spec)) == [
        "dose_related_change:lb:ALT:F:main",
        "dose_related_change:lb:ALT:M:main",
    ]
    keys = by_key(analyse(build(spec), config()))
    assert [g.n for g in keys["lb:ALT:M:main"].groups] == [10, 10, 10, 10]
    assert [g.n for g in keys["lb:ALT:M:recovery"].groups] == [5, 5, 5, 5]
    assert keys["lb:ALT:M:recovery"].check is None


def test_clean_studies_raise_little() -> None:
    # No effect in five clean studies of 24 endpoints each: chance findings stay rare (the
    # null evaluation on the real study measures the rate properly).
    raised = Counter[str]()
    for seed in (41, 42, 43, 44, 45):
        a = analyse(build(StudySpec(seed=seed)), config())
        assert a.problems == ()
        for s in a.summaries:
            if s.check is not None:
                raised[s.check] += 1
    assert raised[DOSE_RELATED_CHANGE] + raised[PAIRWISE_CHANGE] <= 5


def test_toxicokinetic_sets_are_left_out_and_recovery_is_read_from_the_set_name() -> None:
    dm = pl.DataFrame(
        {"USUBJID": ["a", "b", "c", "d"], "SEX": ["M"] * 4, "SETCD": ["1", "1R", "4TK", "4"]}
    )
    rows = []
    for setcd, name, dose, extra in (
        ("1", "Group 1, Control, nonrecovery", "0", {"TCNTRL": "Vehicle"}),
        ("1R", "Group 1, Control, recovery", "0", {"TCNTRL": "Vehicle"}),
        ("4TK", "Group 4, 200 mg/kg, TK", "200", {"TKDESC": "TK"}),
        ("4", "Group 4, 200 mg/kg, nonrecovery", "200", {}),
    ):
        for i, (code, value) in enumerate([("TRTDOS", dose), ("TRTDOSU", "mg/kg"), *extra.items()]):
            rows.append(
                {"SETCD": setcd, "SET": name, "TXSEQ": float(i), "TXPARMCD": code, "TXVAL": value}
            )
    design = read_design(dm, pl.DataFrame(rows), None)
    assert design.animals.select("USUBJID", "phase", "rank").rows() == [
        ("a", "main", 0),
        ("b", "recovery", 0),
        ("d", "main", 1),
    ]
    assert design.dose_unit == "mg/kg"


def test_unusable_records_become_data_quality_findings_and_the_run_completes(
    tmp_path: Path,
) -> None:
    spec = StudySpec(
        seed=37,
        edits=(
            Edit(
                kind="lb_unit",
                sex="M",
                group=1,
                test="GLUC",
                factor=0.0555,
                unit="mmol/L",
                animals=(0, 1),
            ),
            Edit(kind="lb_missing", sex="F", group=0, test="AST", animals=(0, 1, 2, 3, 4, 5, 6, 7)),
        ),
    )
    found = checks(run(tmp_path, spec))
    assert "data_quality:unit_mismatch:lb:GLUC" in found
    assert "data_quality:control_too_few:lb:AST:F:main" in found
    assert not any(
        c.startswith(("dose_related_change:lb:AST:F", "pairwise_change:lb:AST:F")) for c in found
    )


def test_drafts_are_typed_visible_to_the_configured_roles_and_never_say_adverse(
    tmp_path: Path,
) -> None:
    spec = StudySpec(
        seed=38,
        effects=(
            Effect(endpoint="lb:ALT", factors=(1, 1.2, 1.6, 2.5)),
            Effect(endpoint="death", counts=(0, 0, 1, 4)),
        ),
    )
    drafts = run(tmp_path, spec)
    assert drafts
    for d in drafts:
        assert d.kind is FindingKind.TOX_SIGNAL
        assert d.dedup_key.startswith(dedup_prefix(AGENT_ID, VERSION))
        assert d.visible_to == (Role.PORTFOLIO_LEAD, Role.QA_AUDITOR, Role.TOXICOLOGIST)
        text = " ".join([d.title, d.summary, *(e.display or "" for e in d.evidence)])
        assert not re.search(r"advers", text, re.IGNORECASE)
        assert not re.search(r"NOAEL (is|of|=)|LOAEL", text)
        assert all(e.display for e in d.evidence)
        assert any(e.role == "primary" for e in d.evidence)


def test_the_same_study_gives_the_same_drafts_byte_for_byte(tmp_path: Path) -> None:
    spec = StudySpec(seed=39, effects=(Effect(endpoint="om:LIVER", factors=(1, 1.05, 1.15, 1.3)),))
    one = [d.model_dump_json() for d in run(tmp_path / "a", spec)]
    two = [d.model_dump_json() for d in run(tmp_path / "b", spec)]
    assert one == two
    assert json.loads(one[0])["kind"] == "tox_signal"


def test_the_agent_runs_only_on_its_own_send_cut(tmp_path: Path) -> None:
    import structlog

    reader = write_study(tmp_path, StudySpec(seed=40))
    ctx = AgentContext(
        study=reader, clock=FixedClock(T0), config=config(), log=structlog.get_logger()
    )
    with pytest.raises(ValueError, match="SEND study"):
        ToxStudyMonitor().run(cut_trigger("DC-BASE"), ctx)
    with pytest.raises(ValueError, match="trigger is for SEND cut"):
        ToxStudyMonitor().run(send_trigger(cut="OTHER"), ctx)
