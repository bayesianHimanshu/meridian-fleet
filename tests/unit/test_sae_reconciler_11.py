"""safety.sae_reconciler 1.1: data quality, order independence, case versions, duplicates,
optimal pairing, partial dates, terminology, criteria, subject typos, recoded terms, and
the evidence every finding carries. Tables are hand-written (tests.sae_stress_support)."""

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from meridian.agents.base import FindingDraft
from meridian.agents.safety.sae_reconciler import (
    Onset,
    edit_distance,
    gap,
    parse_instant,
    parse_onset,
)
from meridian.domain import safety as s
from meridian.domain.enums import Severity
from meridian.faults.reader import FullCut
from meridian.faults.safety import plant, scenario_set
from meridian.platform.agent_settings import SaeReconcilerConfig, SafetyTerms, load_safety_terms
from tests.sae_stress_support import DEV, check_of, load, run, run_tables, tables
from tests.safety_support import base_cut, reconcile

SITE = "7001"
A, B, C = "201-7001-001", "201-7001-002", "201-7001-003"


def ae(
    u: str, seq: int, term: str, dtc: str, out: str = "RECOVERED/RESOLVED", **kw: Any
) -> dict[str, Any]:
    row = {
        "USUBJID": u,
        "AESEQ": seq,
        "AETERM": term.lower(),
        "AEDECOD": term,
        "AESER": "Y",
        "AESHOSP": "Y",
        "AESTDTC": dtc,
        "AEOUT": out,
        "AEREL": "NONE",
    }
    return {**row, **kw}


def case(
    cid: str,
    u: str,
    term: str,
    onset: str,
    out: str = "RECOVERED/RESOLVED",
    aware: str = "2025-01-20T09:00:00+00:00",
    receipt: str = "2025-01-20T12:00:00+00:00",
    site: str = SITE,
    **kw: Any,
) -> dict[str, Any]:
    row = {
        "CASEID": cid,
        "USUBJID": u,
        "SITEID": site,
        "REPORTED_TERM": term.lower(),
        "PREFERRED_TERM": term,
        "ONSET": onset,
        "SHOSP": "Y",
        "OUTCOME": out,
        "CAUSALITY": "NONE",
        "AWARE_DTC": aware,
        "RECEIPT_DTC": receipt,
    }
    return {**row, **kw}


def spec(
    aes: list[dict[str, Any]], cases: list[dict[str, Any]], subjects: tuple[str, ...] = (A, B, C)
) -> dict[str, Any]:
    return {
        "dm": [{"USUBJID": u, "SITEID": SITE} for u in subjects],
        "ae": aes,
        "safety_cases": cases,
    }


def checks(drafts: list[FindingDraft]) -> set[str]:
    return {check_of(d) for d in drafts}


# ---- Part 1: one bad record never fails the cut


def _base() -> dict[str, Any]:
    """A pairs cleanly; B has a real outcome mismatch that must survive A's bad field."""
    return spec(
        [ae(A, 1, "FALL", "2025-01-10"), ae(B, 1, "RASH", "2025-01-12")],
        [
            case("SC-7A", A, "FALL", "2025-01-10"),
            case("SC-7B", B, "RASH", "2025-01-12", out="FATAL"),
        ],
    )


BAD_FIELDS = [
    # (table, field, bad value, record key, excluded from matching)
    ("ae", "USUBJID", "", "?/1", True),
    ("ae", "AESEQ", "x", f"{A}/?", True),
    ("ae", "AEDECOD", "", f"{A}/1", True),
    ("ae", "AESTDTC", "10 Jan 2025", f"{A}/1", True),
    ("ae", "AEOUT", "BETTER", f"{A}/1", False),
    ("ae", "AEREL", "MAYBE", f"{A}/1", False),
    ("ae", "AESLIFE", "U", f"{A}/1", False),
    ("safety_cases", "CASEID", "", "unidentified-1", True),
    ("safety_cases", "USUBJID", "", "SC-7A", True),
    ("safety_cases", "PREFERRED_TERM", "", "SC-7A", True),
    ("safety_cases", "ONSET", "2025-13-01", "SC-7A", True),
    ("safety_cases", "AWARE_DTC", "", "SC-7A", False),
    ("safety_cases", "RECEIPT_DTC", "2025-01-20", "SC-7A", False),
    ("safety_cases", "OUTCOME", "BETTER", "SC-7A", False),
    ("safety_cases", "CAUSALITY", "MAYBE", "SC-7A", False),
    ("safety_cases", "SLIFE", "U", "SC-7A", False),
]


@pytest.mark.parametrize(("table", "column", "value", "record", "excluded"), BAD_FIELDS)
def test_a_bad_field_gives_a_data_quality_finding_and_the_rest_is_reconciled(
    table: str, column: str, value: str, record: str, excluded: bool
) -> None:
    sp = _base()
    sp[table][0][column] = value
    drafts = run(sp)
    found = checks(drafts)
    (dq,) = [d for d in drafts if check_of(d) == f"{s.DATA_QUALITY}:{record}#{column}"]
    assert dq.severity is Severity.MEDIUM
    assert column in dq.summary
    assert {e.statistic: e.display for e in dq.evidence}["data_quality:field"] == column
    assert f"{s.OUTCOME_MISMATCH}:{B}/1" in found  # every other record is still reconciled
    a_findings = {c for c in found if A in c or "SC-7A" in c or "?/1" in c}
    if excluded:
        assert any(c.startswith((s.SAE_MISSING, s.CASE_WITHOUT_SAE)) for c in found)
    else:  # A still pairs; the field's own check is skipped, nothing else is reported
        assert a_findings == {f"{s.DATA_QUALITY}:{record}#{column}"}


def test_dev_case_16_no_longer_fails_the_cut() -> None:
    sp = load(DEV / "16_missing_awareness.json")
    assert checks(run(sp)) == {
        f"{s.DATA_QUALITY}:SC-ST16A#AWARE_DTC",
        f"{s.OUTCOME_MISMATCH}:201-9016-002/1",
    }


# ---- Part 1: order independence


def _shuffled(t: dict[str, pl.DataFrame], seed: int) -> dict[str, pl.DataFrame]:
    return {
        name: df.sample(fraction=1.0, shuffle=True, seed=seed + i).select(
            df.columns[::-1] if seed % 2 else df.columns
        )
        for i, (name, df) in enumerate(sorted(t.items()))
    }


def _dump(drafts: list[FindingDraft]) -> list[str]:
    return sorted(d.model_dump_json() for d in drafts)


SEEDS = (1, 2, 3, 4, 5)
DEV_CASES = sorted(DEV.glob("*.json"))


@pytest.mark.parametrize("path", DEV_CASES, ids=[p.stem for p in DEV_CASES])
def test_every_dev_stress_case_is_invariant_to_row_order(path: Path) -> None:
    t = tables(load(path))
    reference = _dump(run_tables(t))
    for seed in SEEDS:
        assert _dump(run_tables(_shuffled(t, seed))) == reference, seed


def test_dev_case_5_does_not_depend_on_which_version_comes_first() -> None:
    sp = load(DEV / "05_followup_same_case_id.json")
    forward = checks(run(sp))
    sp["safety_cases"] = sp["safety_cases"][::-1]
    assert checks(run(sp)) == forward == set()


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> FullCut:
    return base_cut(tmp_path_factory.mktemp("sae11"))


def test_every_generated_dev_cut_is_invariant_to_row_order(base: FullCut) -> None:
    dm = base.blinded["dm"]
    for sc in scenario_set("dev", base):
        planted = plant(sc, base)
        reference = _dump(reconcile(planted.ae, planted.cases, dm, cut_id=sc.cut_id))
        for seed in SEEDS[:3]:
            shuffled = [
                df.sample(fraction=1.0, shuffle=True, seed=seed)
                for df in (planted.ae, planted.cases, dm)
            ]
            got = _dump(reconcile(shuffled[0], shuffled[1], shuffled[2], cut_id=sc.cut_id))
            assert got == reference, (sc.cut_id, seed)


# ---- Part 2: follow-ups and duplicates


def test_the_latest_version_is_the_case_and_timeliness_is_the_initial_report() -> None:
    initial = case("SC-7V", A, "FALL", "2025-01-10", out="NOT RECOVERED/NOT RESOLVED")
    follow = case(
        "SC-7V", A, "FALL", "2025-01-10", receipt="2025-02-20T12:00:00+00:00", CASE_VERSION=2
    )
    for rows in ([initial, follow], [follow, initial]):
        drafts = run(spec([ae(A, 1, "FALL", "2025-01-10")], rows))
        assert checks(drafts) == set()  # no outcome mismatch, no orphan, not late
    # Same receipt: the version number decides.
    same = {**follow, "RECEIPT_DTC": initial["RECEIPT_DTC"]}
    assert checks(run(spec([ae(A, 1, "FALL", "2025-01-10")], [same, initial]))) == set()
    # A late initial report stays late whatever the follow-up says.
    late_initial = {**initial, "RECEIPT_DTC": "2025-01-23T12:00:00+00:00", "CASE_VERSION": 1}
    drafts = run(spec([ae(A, 1, "FALL", "2025-01-10")], [follow, late_initial]))
    assert checks(drafts) == {f"{s.LATE_REPORT}:SC-7V"}
    (late,) = drafts
    assert "CASEID=SC-7V; VERSION=2" in late.evidence[0].selector


def test_a_probable_duplicate_is_one_finding_not_an_orphan() -> None:
    aes = [ae(A, 1, "PNEUMONIA", "2025-03-01")]
    one = case("SC-7P", A, "PNEUMONIA", "2025-03-01")
    again = case(
        "SC-7Q",
        A,
        "PNEUMONIA",
        "2025-03-03",
        aware="2025-03-10T09:00:00+00:00",
        receipt="2025-03-10T12:00:00+00:00",
    )
    assert checks(run(spec(aes, [one, again]))) == {f"{s.PROBABLE_DUPLICATE}:SC-7P+SC-7Q"}
    # Two events, two cases: no duplicate.
    two = [*aes, ae(A, 2, "PNEUMONIA", "2025-03-03")]
    assert checks(run(spec(two, [one, again]))) == set()
    # No event at all: one orphan and its duplicate.
    non_serious = [ae(A, 1, "PNEUMONIA", "2024-01-01", AESER="N")]
    assert checks(run(spec(non_serious, [one, again]))) == {
        f"{s.CASE_WITHOUT_SAE}:SC-7P",
        f"{s.PROBABLE_DUPLICATE}:SC-7P+SC-7Q",
    }
    # Outside the window it is a second orphan.
    far = case("SC-7R", A, "PNEUMONIA", "2025-03-20")
    assert checks(run(spec(aes, [one, far]))) == {f"{s.CASE_WITHOUT_SAE}:SC-7R"}


# ---- Part 2: optimal pairing


def test_dev_case_8_pairs_by_content() -> None:
    assert checks(run(load(DEV / "08_same_term_two_days_apart_wrong_date.json"))) == {
        f"{s.ONSET_MISMATCH}:201-9008-001/2"
    }


def test_dev_case_10_is_one_onset_mismatch_within_the_7_day_window() -> None:
    sp = load(DEV / "10_onset_four_days.json")
    assert checks(run(sp)) == {f"{s.ONSET_MISMATCH}:201-9010-001/1"}
    narrow = SaeReconcilerConfig(fallback_window_days=3)
    assert checks(run(sp, narrow)) == {
        f"{s.SAE_MISSING}:201-9010-001/1",
        f"{s.CASE_WITHOUT_SAE}:SC-ST10A",
    }


def test_three_same_term_events_pair_optimally() -> None:
    nr, rg = "NOT RECOVERED/NOT RESOLVED", "RECOVERING/RESOLVING"
    aes = [
        ae(A, 1, "SYNCOPE", "2025-04-02"),
        ae(A, 2, "SYNCOPE", "2025-04-06", out=nr),
        ae(A, 3, "SYNCOPE", "2025-04-08", out=rg),
    ]
    # The second event's case was entered on the third event's day; the third event's case
    # one day late. Greedy by date would give the second event the wrong case.
    cases = [
        case("SC-7X", A, "SYNCOPE", "2025-04-02"),
        case("SC-7Y", A, "SYNCOPE", "2025-04-08", out=nr),
        case("SC-7Z", A, "SYNCOPE", "2025-04-09", out=rg),
    ]
    assert checks(run(spec(aes, cases))) == {f"{s.ONSET_MISMATCH}:{A}/2"}


def test_equal_costs_pair_deterministically() -> None:
    aes = [ae(A, 1, "FALL", "2025-05-01"), ae(A, 2, "FALL", "2025-05-01")]
    cases = [case("SC-7M", A, "FALL", "2025-05-01"), case("SC-7N", A, "FALL", "2025-05-01")]
    for rows in (cases, cases[::-1]):
        assert checks(run(spec(aes, rows))) == set()


# ---- Part 2: partial dates


def test_onset_intervals_and_gaps() -> None:
    month = parse_onset("2025-02")
    year = parse_onset("2025")
    day = parse_onset("2025-02-28T23:00:00+00:00")
    assert month == Onset(date(2025, 2, 1), date(2025, 2, 28), "2025-02")
    assert year is not None
    assert (year.lo, year.hi) == (date(2025, 1, 1), date(2025, 12, 31))
    assert day is not None
    assert not day.partial
    assert gap(month, day) == 0
    assert gap(month, Onset(date(2025, 3, 3), date(2025, 3, 3), "x")) == 3
    for bad in ("", "2025-13", "13/02/2025", "2025-02-30", "2025-02-28 10:00"):
        assert parse_onset(bad) is None, bad
    assert parse_instant("2025-02-28T10:00") is not None
    assert parse_instant("2025-02-28") is None


def test_a_partial_date_pair_is_a_note_not_a_mismatch() -> None:
    inside = spec([ae(A, 1, "FALL", "2025-06")], [case("SC-7D", A, "FALL", "2025-06-17")])
    (note,) = run(inside)
    assert check_of(note) == f"{s.PARTIAL_DATE}:{A}/1"
    assert note.severity is Severity.LOW
    year = spec([ae(A, 1, "FALL", "2025-06-17")], [case("SC-7D", A, "FALL", "2025")])
    assert checks(run(year)) == {f"{s.PARTIAL_DATE}:{A}/1"}
    # Just outside the month: an onset mismatch (3 days), not a note.
    near = spec([ae(A, 1, "FALL", "2025-06")], [case("SC-7D", A, "FALL", "2025-07-03")])
    assert checks(run(near)) == {f"{s.ONSET_MISMATCH}:{A}/1"}
    # Far outside: missing plus orphan.
    far = spec([ae(A, 1, "FALL", "2025-06")], [case("SC-7D", A, "FALL", "2025-08-20")])
    assert checks(run(far)) == {f"{s.SAE_MISSING}:{A}/1", f"{s.CASE_WITHOUT_SAE}:SC-7D"}


# ---- Part 2: terminology


def test_the_committed_terminology_maps_synonyms_and_classes() -> None:
    terms = load_safety_terms()
    assert terms.outcome_of(" recovered ") == "RECOVERED/RESOLVED"
    assert terms.outcome_of("Not  Recovered") == "NOT RECOVERED/NOT RESOLVED"
    assert terms.outcome_of("") == ""
    assert terms.outcome_of("IMPROVED A LOT") is None
    assert terms.causality_of("Probably related") == "PROBABLE"
    assert terms.causality_class("PROBABLE") == "RELATED"
    assert terms.causality_class("REMOTE") == "NOT RELATED"


def test_terminology_must_be_consistent() -> None:
    good = load_safety_terms().model_dump()
    clash = {**good, "outcome": {**good["outcome"], "FATAL": ["RECOVERED"]}}
    with pytest.raises(ValueError, match="two canonical"):
        SafetyTerms.model_validate(clash)
    unclassed = {**good, "causality_binary": {"RELATED": ["POSSIBLE"], "NOT RELATED": ["NONE"]}}
    with pytest.raises(ValueError, match="binary class"):
        SafetyTerms.model_validate(unclassed)


def test_vocabulary_variants_agree_and_real_changes_do_not() -> None:
    def one(out: str, cau: str, case_out: str, case_cau: str) -> set[str]:
        return checks(
            run(
                spec(
                    [ae(A, 1, "FALL", "2025-01-10", out=out, AEREL=cau)],
                    [case("SC-7T", A, "FALL", "2025-01-10", out=case_out, CAUSALITY=case_cau)],
                )
            )
        )

    assert one("RECOVERED/RESOLVED", "PROBABLE", "Recovered", "Related") == set()
    assert one("RECOVERED/RESOLVED", "REMOTE", "resolved", "unlikely") == set()
    assert one("RECOVERED/RESOLVED", "PROBABLE", "Recovered", "Possible") == {
        f"{s.CAUSALITY_MISMATCH}:{A}/1"
    }
    assert one("RECOVERED/RESOLVED", "NONE", "Recovered", "Related") == {
        f"{s.CAUSALITY_MISMATCH}:{A}/1"
    }
    assert one("RECOVERED/RESOLVED", "NONE", "Died", "None") == {f"{s.OUTCOME_MISMATCH}:{A}/1"}
    # An unknown value is a data-quality finding, never a mismatch.
    assert one("RECOVERED/RESOLVED", "NONE", "Much better", "None") == {
        f"{s.DATA_QUALITY}:SC-7T#OUTCOME"
    }


# ---- Part 2: seriousness criteria


def test_seriousness_criteria_are_compared_flag_by_flag() -> None:
    aes = [ae(A, 1, "FALL", "2025-01-10", AESDTH="Y")]
    drafts = run(spec(aes, [case("SC-7C", A, "FALL", "2025-01-10", SDTH="N")]))
    (d,) = drafts
    assert check_of(d) == f"{s.CRITERIA_MISMATCH}:{A}/1"
    assert {e.statistic: e.display for e in d.evidence} == {
        "criteria:clinical": "AESDTH=Y",
        "criteria:safety_db": "SDTH=N",
    }
    # A criterion one side does not record (the pilot has no AESMIE) is not compared.
    assert (
        checks(
            run(
                spec(
                    [ae(A, 1, "FALL", "2025-01-10")],
                    [case("SC-7C", A, "FALL", "2025-01-10", SMIE="Y")],
                )
            )
        )
        == set()
    )


# ---- Part 2: subject typos and recoded terms


def test_edit_distance_counts_a_transposition_as_one() -> None:
    assert edit_distance("201-7001-012", "201-7001-021") == 1
    assert edit_distance("201-7001-012", "201-7001-013") == 1
    assert edit_distance("201-7001-012", "201-7001-0123") == 1
    assert edit_distance("201-7001-012", "201-7001-034") == 2


def test_a_subject_typo_with_one_candidate_is_one_subject_mismatch() -> None:
    # 201-7001-020 is not enrolled; B (201-7001-002) is one transposition away, A and C two.
    assert [edit_distance("201-7001-020", u) for u in (A, B, C)] == [2, 1, 2]
    drafts = run(
        spec(
            [ae(B, 1, "RASH", "2025-02-02")], [case("SC-7S", "201-7001-020", "RASH", "2025-02-02")]
        )
    )
    (d,) = drafts
    assert check_of(d) == f"{s.SUBJECT_MISMATCH}:SC-7S"
    assert d.severity is Severity.HIGH
    assert B in d.title
    assert {e.statistic: e.display for e in d.evidence}["subject:clinical"] == B


def test_a_subject_typo_with_two_candidates_is_not_guessed() -> None:
    # 201-7001-004 is one substitution from both A (201-7001-001) and B (201-7001-002).
    aes = [ae(A, 1, "RASH", "2025-02-02"), ae(B, 1, "RASH", "2025-02-02")]
    drafts = run(spec(aes, [case("SC-7S", "201-7001-004", "RASH", "2025-02-02")]))
    assert checks(drafts) == {
        f"{s.SAE_MISSING}:{A}/1",
        f"{s.SAE_MISSING}:{B}/1",
        f"{s.CASE_WITHOUT_SAE}:SC-7S",
    }


def test_a_subject_typo_needs_the_same_site_and_an_unenrolled_subject() -> None:
    other_site = case("SC-7S", "201-7001-020", "RASH", "2025-02-02", site="7002")
    drafts = run(spec([ae(B, 1, "RASH", "2025-02-02")], [other_site]))
    assert checks(drafts) == {f"{s.SAE_MISSING}:{B}/1", f"{s.CASE_WITHOUT_SAE}:SC-7S"}
    # C is enrolled: its case is C's, not a typo for B.
    drafts = run(spec([ae(B, 1, "RASH", "2025-02-02")], [case("SC-7S", C, "RASH", "2025-02-02")]))
    assert checks(drafts) == {f"{s.SAE_MISSING}:{B}/1", f"{s.CASE_WITHOUT_SAE}:SC-7S"}


def test_a_recoded_term_is_one_term_mismatch() -> None:
    aes = [ae(A, 1, "HYPOTENSION", "2025-03-03")]
    recoded = case("SC-7H", A, "BLOOD PRESSURE DECREASED", "2025-03-04")
    (d,) = run(spec(aes, [recoded]))
    assert check_of(d) == f"{s.TERM_MISMATCH}:{A}/1"
    assert d.severity is Severity.MEDIUM
    # Two days apart, or a second unmatched SAE: no term mismatch.
    later = {**recoded, "ONSET": "2025-03-05"}
    assert checks(run(spec(aes, [later]))) == {
        f"{s.SAE_MISSING}:{A}/1",
        f"{s.CASE_WITHOUT_SAE}:SC-7H",
    }
    two = [*aes, ae(A, 2, "NAUSEA", "2025-03-20")]
    assert f"{s.TERM_MISMATCH}:{A}/1" not in checks(run(spec(two, [recoded])))


# ---- Part 2: evidence


def test_every_subject_level_finding_names_its_records_in_the_evidence() -> None:
    drafts = [d for p in DEV_CASES for d in run(load(p))]
    assert {check_of(d).split(":")[0] for d in drafts} >= {
        s.DATA_QUALITY,
        s.PROBABLE_DUPLICATE,
        s.PARTIAL_DATE,
        s.CRITERIA_MISMATCH,
        s.SUBJECT_MISMATCH,
        s.TERM_MISMATCH,
        s.ONSET_MISMATCH,
        s.LATE_REPORT,
    }
    for d in drafts:
        selectors = " | ".join(e.selector for e in d.evidence)
        name = check_of(d).split(":")[0]
        if name == s.LATE_SITE:
            assert "USUBJID" not in selectors
            continue
        assert "USUBJID=2" in selectors, d.dedup_key
        if "CASEID=" in selectors:
            assert "; VERSION=" in selectors, d.dedup_key
        if "AESEQ=" in selectors:
            assert "AESEQ=" in selectors
    pairs = [d for d in drafts if check_of(d).split(":")[0] in s.CLINICAL_ANCHORED]
    assert pairs
    assert all(any("AESEQ=" in e.selector for e in d.evidence) for d in pairs)


def test_a_late_report_gives_one_delay_form_and_names_both_records() -> None:
    late = case("SC-7L", A, "FALL", "2025-01-10", receipt="2025-01-25T15:00:00+00:00")
    (draft,) = run(spec([ae(A, 3, "FALL", "2025-01-10")], [late]))
    assert check_of(draft) == f"{s.LATE_REPORT}:SC-7L"
    # 2025-01-20T09:00 to 2025-01-25T15:00 is 126 hours.
    assert (
        draft.title == "Safety case SC-7L reached the sponsor 126 hours (5.2 days) after awareness"
    )
    assert "126 hours (5.2 days) later" in draft.summary
    by_stat = {e.statistic: e for e in draft.evidence}
    assert by_stat["report_delay_hours"].display == "126 hours (5.2 days) after awareness"
    assert by_stat["case"].display == f"case SC-7L, version 1, subject {A}"
    assert "CASEID=SC-7L; VERSION=1" in by_stat["case"].selector
    assert by_stat["clinical_record"].selector == f"USUBJID={A}; AESEQ=3; AESER=Y"
    # Without a paired clinical record there is no clinical evidence to name.
    alone = case("SC-7M", B, "RASH", "2025-01-12", receipt="2025-01-25T15:00:00+00:00")
    drafts = run(spec([], [alone]))
    (late_alone,) = [d for d in drafts if check_of(d) == f"{s.LATE_REPORT}:SC-7M"]
    assert "clinical_record" not in {e.statistic for e in late_alone.evidence}
