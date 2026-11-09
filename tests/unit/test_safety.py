"""The synthetic safety database, safety faults and safety.sae_reconciler."""

from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest

from meridian.agents.safety.sae_reconciler import AGENT_ID, SaeReconciler
from meridian.data.blinding import BlindingViolationError
from meridian.data.cut_writer import write_cut
from meridian.domain import safety as s
from meridian.domain.enums import Role, Severity
from meridian.faults.catalog import FaultError
from meridian.faults.reader import FullCut
from meridian.faults.safety import (
    FAULT_TYPES,
    LATE_SITE_FAULT,
    SafetyFault,
    SafetyScenario,
    expected,
    plant,
    scenario_set,
)
from meridian.platform.agent_settings import SaeReconcilerConfig
from meridian.platform.clock import FixedClock
from meridian.safetydb.generate import (
    DATETIME,
    HARMLESS,
    TERM_CASE,
    VERBATIM,
    clinical_saes,
    generate,
    snapshot_seed,
    split,
)
from tests.rbqm_support import NOW, STUDY
from tests.safety_support import base_cut, checks, reconcile


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> FullCut:
    return base_cut(tmp_path_factory.mktemp("safety"))


def _run(
    base: FullCut, ae: pl.DataFrame, cases: pl.DataFrame, **cfg: object
) -> set[tuple[str, str]]:
    config = SaeReconcilerConfig.model_validate(cfg)
    return checks(reconcile(ae, cases, base.blinded["dm"], config))


def test_the_fixture_has_serious_aes_at_several_sites(base: FullCut) -> None:
    saes = clinical_saes(base.blinded["ae"])
    sites = base.blinded["dm"].filter(pl.col("USUBJID").is_in(saes["USUBJID"].to_list()))
    assert saes.height >= 10
    assert sites["SITEID"].n_unique() >= 4


# ---- generation


def test_generation_is_deterministic_per_seed(base: FullCut) -> None:
    a, b = generate(base, 42), generate(base, 42)
    assert a.cases.equals(b.cases, null_equal=True)
    assert a.harmless == b.harmless
    c = generate(base, 43)
    assert not a.cases.equals(c.cases, null_equal=True)
    assert snapshot_seed("DC-BASE") == snapshot_seed("DC-BASE") != snapshot_seed("DC-OTHER")


def test_clean_generation_has_one_case_per_sae_and_timely_receipt(base: FullCut) -> None:
    export = generate(base, snapshot_seed("DC-BASE"))
    saes = clinical_saes(base.blinded["ae"])
    assert export.cases.height == saes.height
    assert export.cases[s.CASEID].n_unique() == saes.height
    for row in export.cases.iter_rows(named=True):
        onset = s.onset_day(row[s.ONSET])
        aware, receipt = s.parse_dtc(row[s.AWARE_DTC]), s.parse_dtc(row[s.RECEIPT_DTC])
        assert onset is not None
        assert timedelta(0) <= aware.date() - onset <= timedelta(days=3)
        assert timedelta(0) < receipt - aware < timedelta(hours=24)


def test_clean_generation_reconciles_to_zero(base: FullCut) -> None:
    export = generate(base, snapshot_seed("DC-BASE"))
    assert _run(base, base.blinded["ae"], export.cases) == set()


@pytest.mark.parametrize("kind", HARMLESS)
def test_each_harmless_difference_on_every_case_is_not_flagged(base: FullCut, kind: str) -> None:
    export = generate(base, 7)
    ae = clinical_saes(base.blinded["ae"])
    by_case = {
        r[s.CASEID]: r for r in export.cases.iter_rows(named=True)
    }  # rebuild every case with this difference only
    rows = []
    for row in by_case.values():
        clinical = ae.filter(
            (pl.col("USUBJID") == row[s.USUBJID])
            & (pl.col("AEDECOD") == s.norm_text(row[s.PREFERRED_TERM]))
        ).row(0, named=True)
        row = {**row, s.PREFERRED_TERM: clinical["AEDECOD"], s.REPORTED_TERM: clinical["AETERM"]}
        row[s.ONSET] = clinical["AESTDTC"]
        if kind == TERM_CASE:
            row[s.PREFERRED_TERM] = (
                "  " + str(clinical["AEDECOD"]).lower().replace(" ", "   ") + " "
            )
        elif kind == VERBATIM:
            row[s.REPORTED_TERM] = f"site wording: {str(clinical['AETERM']).lower()} (verbatim)"
        elif kind == DATETIME:
            row[s.ONSET] = f"{clinical['AESTDTC']}T23:59"
        rows.append(row)
    cases = pl.DataFrame(rows, schema=export.cases.schema)
    assert cases[s.PREFERRED_TERM].to_list() != ae["AEDECOD"].to_list()  # really different
    assert _run(base, base.blinded["ae"], cases) == set()


def test_generated_harmless_differences_are_present_and_real(base: FullCut) -> None:
    export = generate(base, 11)
    assert all(export.harmless[h] for h in HARMLESS)
    cases = {r[s.CASEID]: r for r in export.cases.iter_rows(named=True)}
    for cid in export.harmless[TERM_CASE]:
        term = cases[cid][s.PREFERRED_TERM]
        assert term != s.norm_text(term)
        assert s.norm_text(term) in s.norm_text(term)
    for cid in export.harmless[DATETIME]:
        assert "T" in cases[cid][s.ONSET]


# ---- blinding


def test_the_treatment_field_is_split_into_unblinded(base: FullCut) -> None:
    export = generate(base, 3)
    assert export.cases[s.TREATMENT].drop_nulls().len() > 0  # some cases carry it
    blinded, unblinded = split(export.cases)
    assert s.TREATMENT not in blinded.columns
    assert unblinded.columns == [s.CASEID, s.TREATMENT]
    assert unblinded.height == export.cases[s.TREATMENT].drop_nulls().len()


def test_the_cut_writer_refuses_the_treatment_field_in_blinded(tmp_path: Path) -> None:
    frame = pl.DataFrame({s.CASEID: ["SC-1"], s.TREATMENT: ["Placebo"]})
    with pytest.raises(BlindingViolationError, match="TRTA"):
        write_cut(
            curated_root=tmp_path,
            study_id=STUDY,
            cut_id="DC-X",
            blinded={s.SAFETY_CASES: frame},
            ops={},
            unblinded={},
            created_at=NOW,
            seed=1,
            seed_record={},
            sources=[],
        )
    assert not (tmp_path / STUDY / "DC-X").exists()


def test_a_snapshot_keeps_treatment_out_of_every_blinded_file(tmp_path: Path) -> None:
    from meridian.safetydb.generate import ensure_clean_snapshot

    cut = base_cut(tmp_path)
    ensure_clean_snapshot(
        safety_root=tmp_path / "safety", study_id=STUDY, cut=cut, clock=FixedClock(NOW)
    )
    snap = tmp_path / "safety" / STUDY / "DC-BASE"
    blinded = pl.read_parquet(snap / "blinded" / f"{s.SAFETY_CASES}.parquet")
    unblinded = pl.read_parquet(snap / "unblinded" / f"{s.SAFETY_UNBLINDED}.parquet")
    treatments = set(unblinded[s.TREATMENT].to_list())
    assert treatments
    assert s.TREATMENT not in blinded.columns
    for path in (snap / "blinded").glob("*.parquet"):
        text = pl.read_parquet(path).write_csv()
        assert not any(t in text for t in treatments), path


# ---- the reconciler on single faults


def _scenario(*faults: SafetyFault, seed: int = 9) -> SafetyScenario:
    return SafetyScenario(
        set_name="dev", index=1, seed=seed, base_cut="DC-BASE", augment=0, faults=faults
    )


FAULTS = [
    SafetyFault(type=s.SAE_MISSING, params={}),
    SafetyFault(type=s.CASE_WITHOUT_SAE, params={"variant": 0.0}),
    SafetyFault(type=s.CASE_WITHOUT_SAE, params={"variant": 1.0}),
    SafetyFault(type=s.ONSET_MISMATCH, params={"shift_days": 2.0}),
    SafetyFault(type=s.ONSET_MISMATCH, params={"shift_days": -3.0}),
    SafetyFault(type=s.OUTCOME_MISMATCH, params={}),
    SafetyFault(type=s.CAUSALITY_MISMATCH, params={}),
    SafetyFault(type=s.LATE_REPORT, params={"delay_hours": 30.0}),
    SafetyFault(type=LATE_SITE_FAULT, params={"delay_hours": 48.0}),
]


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f"{f.type}-{sorted(f.params.items())}")
def test_each_fault_is_flagged_with_its_check_and_nothing_else(
    base: FullCut, fault: SafetyFault
) -> None:
    planted = plant(_scenario(fault), base)
    truth = {(r.kri or "", r.target) for r in planted.records}
    assert truth
    assert _run(base, planted.ae, planted.cases) == truth


def test_several_faults_in_one_cut_are_each_flagged(base: FullCut) -> None:
    sc = _scenario(
        *(
            SafetyFault(type=t, params=FAULTS[[f.type for f in FAULTS].index(t)].params)
            for t in FAULT_TYPES[:3]
        )
    )
    planted = plant(sc, base)
    assert _run(base, planted.ae, planted.cases) == {
        (r.kri or "", r.target) for r in planted.records
    }


def test_downgraded_seriousness_says_so_in_the_evidence(base: FullCut) -> None:
    planted = plant(_scenario(SafetyFault(type=s.CASE_WITHOUT_SAE, params={"variant": 1.0})), base)
    drafts = reconcile(planted.ae, planted.cases, base.blinded["dm"])
    (draft,) = drafts
    clinical = next(e for e in draft.evidence if e.statistic == "seriousness:clinical")
    assert clinical.display is not None
    assert "non-serious" in clinical.display
    assert draft.severity is Severity.HIGH


def test_fallback_matching_gives_a_field_mismatch_not_two_missing_records(base: FullCut) -> None:
    export = generate(base, 5)
    first = export.cases.row(0, named=True)
    moved = s.onset_day(first[s.ONSET])
    assert moved is not None

    def shift(days: int) -> pl.DataFrame:
        text = (moved + timedelta(days=days)).isoformat()
        return export.cases.with_columns(
            pl.when(pl.col(s.CASEID) == first[s.CASEID])
            .then(pl.lit(text))
            .otherwise(pl.col(s.ONSET))
            .alias(s.ONSET)
        )

    one_day = _run(base, base.blinded["ae"], shift(1))
    assert one_day == set()  # within the 1-day tolerance
    three = _run(base, base.blinded["ae"], shift(3))
    assert {c for c, _ in three} == {s.ONSET_MISMATCH}
    # 1.1: within the 7-day window the cost decides; 4 days is one onset mismatch.
    four = _run(base, base.blinded["ae"], shift(4))
    assert {c for c, _ in four} == {s.ONSET_MISMATCH}
    # Beyond the window the same case reads as missing plus orphan.
    eight = _run(base, base.blinded["ae"], shift(8))
    assert {c for c, _ in eight} == {s.SAE_MISSING, s.CASE_WITHOUT_SAE}
    narrow = _run(base, base.blinded["ae"], shift(4), fallback_window_days=3)
    assert {c for c, _ in narrow} == {s.SAE_MISSING, s.CASE_WITHOUT_SAE}


def test_a_partial_clinical_onset_matches_through_a_partial_date_note(base: FullCut) -> None:
    export = generate(base, 5)
    saes = clinical_saes(base.blinded["ae"])
    u, q = saes.row(0)[saes.columns.index("USUBJID")], saes.row(0)[saes.columns.index("AESEQ")]
    ae = base.blinded["ae"].with_columns(
        pl.when((pl.col("USUBJID") == u) & (pl.col("AESEQ") == q))
        .then(pl.col("AESTDTC").str.slice(0, 7))
        .otherwise(pl.col("AESTDTC"))
        .alias("AESTDTC")
    )
    found = _run(base, ae, export.cases)
    assert found == {(s.PARTIAL_DATE, f"{u}/{q}")}


def _late(cases: pl.DataFrame, ids: list[str], hours: float = 40.0) -> pl.DataFrame:
    rows = []
    for r in cases.iter_rows(named=True):
        if r[s.CASEID] in ids:
            r = {
                **r,
                s.RECEIPT_DTC: (s.parse_dtc(r[s.AWARE_DTC]) + timedelta(hours=hours)).isoformat(),
            }
        rows.append(r)
    return pl.DataFrame(rows, schema=cases.schema)


def test_site_level_late_reporting_needs_two_late_cases(base: FullCut) -> None:
    export = generate(base, 5)
    by_site = (
        export.cases.group_by(s.SITEID, maintain_order=True).agg(pl.col(s.CASEID)).sort(s.SITEID)
    )
    site, ids = next((site, list(ids)) for site, ids in by_site.iter_rows() if len(ids) >= 3)
    one = _run(base, base.blinded["ae"], _late(export.cases, ids[:1]))
    assert one == {(s.LATE_REPORT, ids[0])}
    two = _run(base, base.blinded["ae"], _late(export.cases, ids[:2]))
    assert two == {(s.LATE_REPORT, ids[0]), (s.LATE_REPORT, ids[1]), (s.LATE_SITE, site)}
    three_needed = _run(base, base.blinded["ae"], _late(export.cases, ids[:2]), site_late_min=3)
    assert (s.LATE_SITE, site) not in three_needed
    exactly = _run(base, base.blinded["ae"], _late(export.cases, ids[:1], hours=24.0))
    assert exactly == set()  # at 24 hours exactly the case is on time


def test_visibility_per_finding_comes_from_the_config(base: FullCut) -> None:
    export = generate(base, 5)
    by_site = (
        export.cases.group_by(s.SITEID, maintain_order=True).agg(pl.col(s.CASEID)).sort(s.SITEID)
    )
    ids = next(list(ids) for _, ids in by_site.iter_rows() if len(ids) >= 2)
    drafts = reconcile(base.blinded["ae"], _late(export.cases, ids[:2]), base.blinded["dm"])
    site = [d for d in drafts if s.LATE_SITE in d.dedup_key]
    subject = [d for d in drafts if s.LATE_SITE not in d.dedup_key]
    assert len(site) == 1
    assert len(subject) == 2
    assert site[0].visible_to == tuple(sorted(SaeReconcilerConfig().site_visible_to))
    assert Role.PORTFOLIO_LEAD in (site[0].visible_to or ())
    for d in subject:
        assert set(d.visible_to or ()) == {
            Role.CLINICAL_OPS_LEAD,
            Role.MEDICAL_MONITOR,
            Role.QA_AUDITOR,
        }
    # The site-level finding names no subject and no case.
    text = site[0].model_dump_json()
    subjects = set(base.blinded["dm"]["USUBJID"].to_list())
    assert not any(u in text for u in subjects)
    assert not any(i in text for i in ids)
    assert set(d.visible_to or ()) <= SaeReconciler.visible_to


def test_subject_visibility_may_not_exceed_site_visibility() -> None:
    with pytest.raises(ValueError, match="more roles"):
        SaeReconcilerConfig(
            subject_visible_to=(Role.PORTFOLIO_LEAD,), site_visible_to=(Role.QA_AUDITOR,)
        )


def test_the_agent_identity() -> None:
    agent = SaeReconciler()
    assert (agent.agent_id, agent.version, agent.zone.value) == (AGENT_ID, "1.1.1", "safety")
    assert Role.TOXICOLOGIST not in agent.visible_to
    assert Role.UNBLINDED_STATISTICIAN not in agent.visible_to


# ---- the evaluation set


def test_the_scenario_set_is_fixed_and_its_ids_come_from_the_canonical_form(base: FullCut) -> None:
    a = scenario_set("dev", base, n=6)
    b = scenario_set("dev", base, n=6)
    assert [x.cut_id for x in a] == [x.cut_id for x in b]
    assert len({x.cut_id for x in a}) == 6
    assert all(1 <= len(x.faults) <= 3 for x in a)
    assert all(len({f.type for f in x.faults}) == len(x.faults) for x in a)
    one = a[0]
    same = SafetyScenario.model_validate_json(one.model_dump_json())
    assert same.cut_id == one.cut_id
    assert same.canonical() == one.canonical()
    assert one.model_copy(update={"seed": one.seed + 1}).cut_id != one.cut_id
    holdout = scenario_set("holdout", base, n=3)
    assert not {x.cut_id for x in holdout} & {x.cut_id for x in a}
    clean = scenario_set("clean", base, n=3)
    assert all(x.faults == () for x in clean)


def test_every_scenario_of_a_small_set_reconciles_exactly(base: FullCut) -> None:
    for sc in scenario_set("dev", base, n=8):
        planted = plant(sc, base)
        assert _run(base, planted.ae, planted.cases) == {
            (r.kri or "", r.target) for r in planted.records
        }, sc.label


def test_planting_on_too_few_saes_fails_loudly(base: FullCut) -> None:
    sc = _scenario(*(SafetyFault(type=s.SAE_MISSING, params={}) for _ in range(500)))
    with pytest.raises(FaultError):
        plant(sc, base)


def test_expected_reads_check_and_record_from_the_ground_truth(base: FullCut) -> None:
    from meridian.faults.inject import GroundTruth

    planted = plant(_scenario(SafetyFault(type=s.LATE_REPORT, params={"delay_hours": 50.0})), base)
    truth = GroundTruth(
        cut_id="DC-X",
        scenario_path="safety:dev/01",
        scenario_sha256="0" * 64,
        seed=1,
        base_cut="DC-BASE",
        base_manifest_sha256="0" * 64,
        faults=planted.records,
    )
    ((check, record),) = expected(truth)
    assert check == s.LATE_REPORT
    assert record.startswith("SC-")
