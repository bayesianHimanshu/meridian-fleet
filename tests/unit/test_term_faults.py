"""The recoded_term and lookalike_term safety faults (step 3b2): the committed pairs, the
terms set, what is planted, and the ground truth."""

import json
from pathlib import Path

import polars as pl
import pytest

from meridian.domain import safety as s
from meridian.faults.catalog import FaultError
from meridian.faults.reader import FullCut
from meridian.faults.safety import (
    FAULT_TYPES,
    TERM_FAULT_TYPES,
    SafetyFault,
    SafetyScenario,
    plant,
    scenario_set,
)
from meridian.faults.term_pairs import (
    EXPECTED_VERDICT,
    LOOKALIKE,
    LOOKALIKE_TERM,
    RECODED,
    RECODED_TERM,
)
from meridian.platform.agent_settings import SaeReconcilerConfig
from tests.safety_support import base_cut, checks, reconcile

REPO = Path(__file__).resolve().parents[2]
DEV = REPO / "data" / "term_pairs" / "dev.jsonl"
SEALED = REPO / "meridian-sealed" / "term_pairs.jsonl"  # the user's; never committed


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> FullCut:
    return base_cut(tmp_path_factory.mktemp("terms"))


def _pairs() -> set[frozenset[str]]:
    return {frozenset(p) for p in (*RECODED, *LOOKALIKE)}


def test_the_pairs_are_distinct_and_of_two_different_terms() -> None:
    assert len(_pairs()) == len(RECODED) + len(LOOKALIKE)
    assert all(a != b and a == a.upper() and b == b.upper() for a, b in (*RECODED, *LOOKALIKE))
    assert EXPECTED_VERDICT == {RECODED_TERM: "yes", LOOKALIKE_TERM: "no"}


def test_no_pair_is_a_pair_of_the_dev_set() -> None:
    dev = set()
    for line in DEV.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        dev.add(frozenset((d["record_a"]["preferred_term"], d["record_b"]["preferred_term"])))
    assert not _pairs() & dev


@pytest.mark.skipif(not SEALED.exists(), reason="the sealed set is only on the user's machine")
def test_no_pair_is_a_pair_of_the_sealed_set() -> None:
    sealed = set()
    for line in SEALED.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            sealed.add(frozenset((str(d["term_a"]).upper(), str(d["term_b"]).upper())))
    assert not _pairs() & sealed  # only the count is ever compared, never printed


def test_the_terms_set_is_fixed_and_plants_only_term_faults(base: FullCut) -> None:
    a = scenario_set("terms", base, n=6)
    assert [x.cut_id for x in a] == [x.cut_id for x in scenario_set("terms", base, n=6)]
    assert all(1 <= len(x.faults) <= 2 for x in a)
    assert {f.type for x in a for f in x.faults} == set(TERM_FAULT_TYPES)
    assert not set(TERM_FAULT_TYPES) & set(FAULT_TYPES)  # the other sets are unchanged
    assert all(x.label.startswith("safety_terms_") for x in a)


def _scenario(*types: str, seed: int = 7) -> SafetyScenario:
    return SafetyScenario(
        set_name="terms",
        index=1,
        seed=seed,
        base_cut="DC-BASE",
        augment=10,
        faults=tuple(SafetyFault(type=t, params={}) for t in types),
    )


@pytest.mark.parametrize("kind", TERM_FAULT_TYPES)
def test_a_term_fault_recodes_one_case_and_the_reconciler_raises_a_term_mismatch(
    base: FullCut, kind: str
) -> None:
    for seed in range(40):  # the synthetic base has few terms of the pairs
        try:
            planted = plant(_scenario(kind, seed=seed), base)
            break
        except FaultError:
            continue
    else:
        pytest.fail(f"no seed planted {kind}")
    (record,) = planted.records
    assert (record.fault_type, record.kri) == (kind, s.TERM_MISMATCH)
    pairs = RECODED if kind == RECODED_TERM else LOOKALIKE
    source, target = pairs[int(record.params["pair"])]
    usubjid, aeseq = record.target.split("/")
    ae = planted.ae.filter((pl.col("USUBJID") == usubjid) & (pl.col("AESEQ") == float(aeseq)))
    clinical = ae.to_dicts()[0]
    assert s.norm_text(clinical["AEDECOD"]) == source  # the clinical record is untouched
    case = planted.cases.filter(
        (pl.col("USUBJID") == usubjid) & (pl.col(s.PREFERRED_TERM) == target)
    ).to_dicts()
    assert len(case) == 1
    if kind == RECODED_TERM:
        assert case[0][s.REPORTED_TERM] == clinical["AETERM"]  # only the coding differs
    else:
        assert case[0][s.REPORTED_TERM] == target.lower()
    found = checks(reconcile(planted.ae, planted.cases, base.blinded["dm"], SaeReconcilerConfig()))
    assert found == {(s.TERM_MISMATCH, record.target)}


def test_every_scenario_of_the_terms_set_reconciles_exactly(base: FullCut) -> None:
    for sc in scenario_set("terms", base, n=4):
        planted = plant(sc, base)
        subjects = [r.target.split("/")[0] for r in planted.records]
        assert len(subjects) == len(set(subjects))  # never two term faults on one subject
        got = checks(
            reconcile(planted.ae, planted.cases, base.blinded["dm"], SaeReconcilerConfig())
        )
        assert got == {(r.kri or "", r.target) for r in planted.records}, sc.label
