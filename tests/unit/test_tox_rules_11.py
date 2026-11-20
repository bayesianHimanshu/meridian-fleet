"""preclinical.tox_study_monitor 1.1.0 rules (docs/preclinical-eval.md, "1.1.0: changes
and targets"), each on a small synthetic study (seeds apart from the stress set's)."""

from pathlib import Path

import pytest

from meridian.agents.preclinical.tox_study_monitor import VERSION
from meridian.preclinical.analysis import analyse, by_key
from meridian.preclinical.rules import EndpointItem, raise_findings
from meridian.preclinical.synth import STUDY, Edit, Effect, LbTest, StudySpec, build
from tests.tox_support import checks, config, run

U = f"{STUDY}-"


def test_the_version_is_1_1_0() -> None:
    assert VERSION == "1.1.0"


def test_an_unscheduled_death_in_a_treated_group_is_raised_with_its_mi_cause(
    tmp_path: Path,
) -> None:
    edit = Edit(
        kind="death",
        sex="M",
        group=3,
        animal=4,
        day=44,
        specimen="KIDNEY",
        finding="NEPHROPATHY",
        category="",
    )
    drafts = run(tmp_path, StudySpec(seed=61, edits=(edit,)))
    death = [d for d in drafts if d.dedup_key.endswith(f"unscheduled_death:{U}4005")]
    assert len(death) == 1
    assert "Kidney: nephropathy" in death[0].summary
    assert "day 44" in death[0].title
    assert death[0].severity == "high"


def test_a_control_death_is_not_raised_and_a_missing_cause_is_said(tmp_path: Path) -> None:
    edits = (
        Edit(kind="death", sex="F", group=0, animal=0, day=30),
        Edit(kind="death", sex="F", group=1, animal=0, day=31),
    )
    drafts = run(tmp_path, StudySpec(seed=62, edits=edits))
    deaths = [d for d in drafts if "unscheduled_death:" in d.dedup_key]
    assert [d.dedup_key.rsplit(":", 1)[1] for d in deaths] == [f"{U}2051"]
    assert "No cause recorded" in deaths[0].summary


def test_one_animal_with_a_neoplasm_in_a_treated_group_is_raised(tmp_path: Path) -> None:
    edits = (
        Edit(
            kind="mi_finding",
            sex="F",
            group=2,
            animal=1,
            specimen="KIDNEY",
            finding="ADENOMA, TUBULAR",
            category="BENIGN",
        ),
        Edit(
            kind="mi_finding",
            sex="M",
            group=0,
            animal=1,
            specimen="LIVER",
            finding="HEPATOCELLULAR CARCINOMA",
            category="MALIGNANT",
        ),
    )
    found = checks(run(tmp_path, StudySpec(seed=63, edits=edits)))
    assert "neoplasm:mi:KIDNEY:ADENOMA, TUBULAR:F" in found
    # In the control only: no neoplasm finding, and never an incidence finding either.
    assert not [c for c in found if "CARCINOMA" in c]


def test_a_term_names_a_neoplasm_when_mirescat_is_empty(tmp_path: Path) -> None:
    edit = Edit(
        kind="mi_finding",
        sex="M",
        group=3,
        animal=0,
        specimen="LIVER",
        finding="HEPATOCELLULAR CARCINOMA",
        category="",
    )
    tables = build(StudySpec(seed=64, edits=(edit,)))
    a = analyse(tables, config())
    s = by_key(a)["mi:LIVER:HEPATOCELLULAR CARCINOMA:M:main"]
    assert s.endpoint.neoplastic
    granuloma = Edit(
        kind="mi_finding",
        sex="M",
        group=3,
        animal=0,
        specimen="LIVER",
        finding="GRANULOMA",
        category="",
    )
    b = analyse(build(StudySpec(seed=64, edits=(granuloma,))), config())
    assert not by_key(b)["mi:LIVER:GRANULOMA:M:main"].endpoint.neoplastic


def test_relative_organ_weights_with_lower_body_weight_are_one_finding_per_sex_and_dose(
    tmp_path: Path,
) -> None:
    spec = StudySpec(seed=65, effects=(Effect(endpoint="bw:GAIN", factors=(1, 1, 1, 0.5)),))
    found = checks(run(tmp_path, spec))
    assert "organ_weight_secondary_to_bw:M:main:r3" in found
    assert "organ_weight_secondary_to_bw:F:main:r3" in found
    assert not [c for c in found if ":rel:" in c]
    drafts = run(tmp_path / "again", spec)
    grouped = next(
        d for d in drafts if d.dedup_key.endswith("organ_weight_secondary_to_bw:M:main:r3")
    )
    assert "may be secondary to lower body weight" in grouped.title
    assert "Liver" in grouped.summary
    assert "Kidney" in grouped.summary


def test_a_relative_weight_with_a_matching_absolute_change_keeps_its_own_finding(
    tmp_path: Path,
) -> None:
    spec = StudySpec(
        seed=66,
        effects=(
            Effect(endpoint="bw:GAIN", factors=(1, 1, 1, 0.5)),
            Effect(endpoint="om:LIVER", factors=(1, 1, 1.2, 1.6)),
        ),
    )
    found = checks(run(tmp_path, spec))
    assert "dose_related_change:om:LIVER:abs:M:main" in found
    assert "dose_related_change:om:LIVER:rel:M:main" in found


def test_recovery_is_evidence_on_the_main_finding(tmp_path: Path) -> None:
    persists = StudySpec(
        seed=67,
        recovery=True,
        effects=(Effect(endpoint="lb:ALT", factors=(1, 1.2, 1.6, 2.5), phase="both"),),
    )
    found = checks(run(tmp_path, persists))
    assert "dose_related_change:lb:ALT:M:main" in found
    assert not [c for c in found if c.endswith(":recovery") and "ALT" in c]
    tables = build(persists)
    a = analyse(tables, config())
    items = {
        i.check: i for i in raise_findings(a, tables, config()).items if isinstance(i, EndpointItem)
    }
    status = items["dose_related_change:lb:ALT:M:main"].recovery
    assert status is not None
    assert status.status == "persists"
    assert status.dose == 100.0
    reverses = persists.model_copy(
        update={"effects": (Effect(endpoint="lb:ALT", factors=(1, 1.2, 1.6, 2.5)),)}
    )
    t2 = build(reverses)
    a2 = analyse(t2, config())
    item = next(
        i
        for i in raise_findings(a2, t2, config()).items
        if isinstance(i, EndpointItem) and i.check == "dose_related_change:lb:ALT:M:main"
    )
    assert item.recovery is not None
    assert item.recovery.status == "reverses"
    drafts = run(tmp_path / "r", reverses)
    alt = next(d for d in drafts if d.dedup_key.endswith("dose_related_change:lb:ALT:M:main"))
    assert "At recovery (100 mg/kg/day): reverses" in alt.summary
    assert any(e.statistic == "recovery_status:lb:ALT" for e in alt.evidence)


def test_a_change_only_at_recovery_is_its_own_finding(tmp_path: Path) -> None:
    spec = StudySpec(
        seed=68,
        recovery=True,
        n_recovery=10,
        effects=(Effect(endpoint="lb:AST", factors=(1, 1.3, 1.8, 2.5), phase="recovery"),),
    )
    found = checks(run(tmp_path, spec))
    assert "recovery_only_change:lb:AST:M:recovery" in found
    assert not [c for c in found if c.startswith("dose_related_change:lb:AST")]


RED = (
    LbTest(code="RBC", category="HEMATOLOGY", unit="10^12/L", mean_m=8.5, mean_f=8.0, cv=0.02),
    LbTest(code="HGB", category="HEMATOLOGY", unit="g/dL", mean_m=15.5, mean_f=15.0, cv=0.02),
    LbTest(code="HCT", category="HEMATOLOGY", unit="%", mean_m=45.0, mean_f=44.0, cv=0.02),
    LbTest(code="ALT", category="CLINICAL CHEMISTRY", unit="U/L", mean_m=40, mean_f=32, cv=0.1),
)


@pytest.mark.parametrize("coherent", [True, False])
def test_red_cell_decreases_of_6_percent_need_a_second_parameter(
    tmp_path: Path, coherent: bool
) -> None:
    down = (1, 1, 1, 0.94)
    effects = [Effect(endpoint="lb:RBC", factors=down)]
    if coherent:
        effects.append(Effect(endpoint="lb:HGB", factors=down))
    found = checks(run(tmp_path, StudySpec(seed=69, lb=RED, effects=tuple(effects))))
    rbc = [c for c in found if ":lb:RBC:M:" in c]
    assert bool(rbc) is coherent
    assert bool([c for c in found if ":lb:HGB:M:" in c]) is coherent


def test_two_animals_in_the_top_group_are_not_a_dose_related_incidence(tmp_path: Path) -> None:
    spec = StudySpec(
        seed=70, effects=(Effect(endpoint="mi:KIDNEY:BASOPHILIA", counts=(0, 0, 0, 2)),)
    )
    a = analyse(build(spec), config())
    s = by_key(a)["mi:KIDNEY:BASOPHILIA:M:main"]
    assert s.trend_p == pytest.approx(45 / 780)
    assert s.check is None
    three = spec.model_copy(
        update={"effects": (Effect(endpoint="mi:KIDNEY:BASOPHILIA", counts=(0, 0, 0, 3)),)}
    )
    assert "dose_related_incidence:mi:KIDNEY:BASOPHILIA:M:main" in checks(run(tmp_path, three))
