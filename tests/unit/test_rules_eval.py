"""Rules versus detectors in the eval: check types, condition truth, rule outcomes."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from meridian.agents.base import FindingDraft
from meridian.agents.rbqm import csm, kri_monitor, qtl_detector
from meridian.agents.rbqm.kri_monitor import KriMonitor
from meridian.agents.rbqm.qtl_detector import QtlDetector
from meridian.data.study_store import ParquetStudyReader
from meridian.domain.dedup import STUDY_SCOPE, parse_dedup_key
from meridian.domain.enums import FindingKind
from meridian.evals.matching import FindingRecord
from meridian.evals.rules import (
    FAULT_CHECK,
    RuleCondition,
    condition_truth,
    evaluate_rules,
    fault_check,
)
from meridian.faults.catalog import REGISTRY, FaultRecord, FaultSpec
from meridian.platform.agent_settings import KriMonitorConfig, QtlDetectorConfig
from meridian.stats.checks import CHECK_TYPES, CheckType, UnknownCheckError, check_type
from tests.agent_support import clean, plant, run_agent
from tests.rbqm_support import STUDY

KRI = KriMonitorConfig()
QTL = QtlDetectorConfig(discontinuation_limit=0.9)


def test_every_agent_check_has_a_check_type() -> None:
    names = {*kri_monitor.CHECKS, *qtl_detector.CHECKS, *csm.CHECKS}
    assert names == set(CHECK_TYPES)
    assert {n for n in names if check_type(n) is CheckType.RULE} == {
        "data_entry_lag",
        "query_aging",
        "premature_discontinuation",
    }


def test_unknown_check_names_raise() -> None:
    with pytest.raises(UnknownCheckError, match="chi_square"):
        check_type("chi_square")
    with pytest.raises(UnknownCheckError):
        fault_check("not_a_fault")


def test_every_fault_type_has_a_known_check() -> None:
    assert set(FAULT_CHECK) == set(REGISTRY)
    for fault_type in REGISTRY:
        check_type(fault_check(fault_type))


def _records(drafts: Sequence[FindingDraft], agent_id: str) -> list[FindingRecord]:
    return [
        FindingRecord(
            seq=i,
            finding_id=f"{agent_id}#{i}",
            agent_id=agent_id,
            agent_version="test",
            kind=d.kind,
            site=d.site_id or STUDY_SCOPE,
            cut_id="C",
            check=parse_dedup_key(d.dedup_key).check,
            severity=d.severity.value,
            score=d.score,
            title=d.title,
        )
        for i, d in enumerate(drafts)
    ]


def _record(fault_type: str, target: str, fault_id: str = "F-001") -> FaultRecord:
    kind = REGISTRY[fault_type]
    return FaultRecord(
        fault_id=fault_id,
        fault_type=fault_type,
        target=target,
        params={},
        expected_kind=kind.expected_kind,
        kri=kind.kri,
        rows_touched={},
        achieved={},
    )


def _slow_site(ops: dict[str, pl.DataFrame], site: str, factor: float) -> dict[str, pl.DataFrame]:
    de = ops["data_entry"].with_columns(
        pl.when(pl.col("SITEID") == site)
        .then(pl.col("entry_lag_days") * factor)
        .otherwise(pl.col("entry_lag_days"))
        .alias("entry_lag_days")
    )
    return {**ops, "data_entry": de}


def _score(
    tmp_path: Path,
    cut: str,
    blinded: dict[str, pl.DataFrame],
    ops: dict[str, pl.DataFrame],
    unb: dict[str, pl.DataFrame],
) -> tuple[tuple[RuleCondition, ...], list[FindingRecord]]:
    findings = _records(
        run_agent(KriMonitor(), KRI, tmp_path, cut, blinded, ops, unb), "rbqm.kri_monitor"
    ) + _records(
        run_agent(QtlDetector(), QTL, tmp_path, cut, blinded, ops, unb), "rbqm.qtl_detector"
    )
    reader = ParquetStudyReader(tmp_path / STUDY, cut)
    return condition_truth(reader, KRI, QTL), findings


def test_natural_breach_on_a_clean_cut_is_correct_not_a_false_positive(tmp_path: Path) -> None:
    blinded, ops, unb = clean()
    ops = _slow_site(ops, "1003", 4.0)  # no planted fault, the site is just slow
    conditions, findings = _score(tmp_path, "DC-N", blinded, ops, unb)
    lag = [c for c in conditions if c.check == "data_entry_lag" and c.met]
    assert [c.site for c in lag] == ["1003"]
    result = evaluate_rules(conditions, findings, [])
    assert result.agreement == 1.0
    (natural,) = result.of("natural_breach")
    assert (natural.check, natural.site) == ("data_entry_lag", "1003")
    assert natural.finding_id is not None
    assert result.of("missing_finding") == result.of("unsupported_finding") == []


def test_sub_threshold_planted_fault_is_below_threshold_by_design(tmp_path: Path) -> None:
    blinded, ops, unb = clean()
    blinded, ops = plant(blinded, ops, "entry_lag_spike", "1002", {"target_median_days": 6.0})
    conditions, findings = _score(tmp_path, "DC-6", blinded, ops, unb)
    faults = [_record("entry_lag_spike", "1002")]
    result = evaluate_rules(conditions, findings, faults)
    (below,) = result.of("below_threshold_by_design")
    assert (below.site, below.fault_id, below.finding_id) == ("1002", "F-001", None)
    assert below.value == pytest.approx(6.0)
    assert result.agreement == 1.0
    assert result.of("planted_breach") == []


def test_planted_breach_and_both_kinds_of_error() -> None:
    def cond(site: str, met: bool) -> RuleCondition:
        return RuleCondition(
            check="query_aging", site=site, value=30.0 if met else 5.0, n=6, threshold=21, met=met
        )

    def found(site: str) -> FindingRecord:
        return FindingRecord(
            seq=1,
            finding_id=f"Q{site}",
            agent_id="rbqm.kri_monitor",
            agent_version="x",
            kind=FindingKind.KRI_BREACH,
            site=site,
            cut_id="C",
            check="query_aging",
            severity="high",
            score=1.0,
            title="t",
        )

    conditions = [cond("1001", True), cond("1002", True), cond("1003", False), cond("1004", False)]
    findings = [found("1001"), found("1003")]
    faults = [_record("query_backlog", "1001")]
    result = evaluate_rules(conditions, findings, faults)
    assert {(o.site, o.outcome) for o in result.outcomes} == {
        ("1001", "planted_breach"),
        ("1002", "missing_finding"),
        ("1003", "unsupported_finding"),
    }
    assert (result.units, result.agreeing) == (4, 2)
    # Detector findings are ignored by the rule scoring.
    detector = found("1004").model_copy(update={"check": "deviations", "finding_id": "D"})
    assert evaluate_rules(conditions, [*findings, detector], faults) == result


def test_qtl_condition_matches_the_detector(tmp_path: Path) -> None:
    blinded, ops, unb = clean()
    spec = FaultSpec(
        fault_id="F-001",
        fault_type="discontinuation_drift",
        target="STUDY",
        params={"target_rate": 0.95},
    )
    out, _ = REGISTRY["discontinuation_drift"].fn(
        {**blinded, **ops}, spec, np.random.default_rng(1)
    )
    blinded = {k: out[k] for k in blinded}
    conditions, findings = _score(tmp_path, "DC-Q", blinded, ops, unb)
    (study,) = [c for c in conditions if c.check == "premature_discontinuation"]
    assert study.met
    assert study.site == STUDY_SCOPE
    result = evaluate_rules(conditions, findings, [_record("discontinuation_drift", "STUDY")])
    assert [o.outcome for o in result.outcomes if o.site == STUDY_SCOPE] == ["planted_breach"]
    assert result.agreement == 1.0
