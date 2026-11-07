from datetime import UTC, datetime
from pathlib import Path

import pytest

from meridian.domain.enums import FindingKind
from meridian.evals.matching import FindingRecord, VersionFilter, match_faults
from meridian.evals.metrics import average_precision, cut_metrics
from meridian.evals.scorecard import (
    CutEvaluation,
    ScorecardExistsError,
    ScorecardStore,
    new_scorecard,
)
from meridian.faults.catalog import FaultRecord
from meridian.platform.clock import FixedClock

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def fault(
    fid: str, kind: FindingKind, target: str, kri: str | None, ftype: str = "t"
) -> FaultRecord:
    return FaultRecord(
        fault_id=fid,
        fault_type=ftype,
        target=target,
        params={},
        expected_kind=kind,
        kri=kri,
        rows_touched={"x": 1},
        achieved={},
    )


def finding(
    fid: str,
    kind: FindingKind,
    site: str,
    check: str,
    score: float,
    agent: str = "rbqm.kri_monitor",
    version: str = "2.0.0",
    seq: int = 1,
) -> FindingRecord:
    return FindingRecord(
        seq=seq,
        finding_id=fid,
        agent_id=agent,
        agent_version=version,
        kind=kind,
        site=site,
        cut_id="C",
        check=check,
        severity="high",
        score=score,
        title="t",
    )


KRI = FindingKind.KRI_BREACH


def test_highest_score_matches_and_duplicates_are_false_positives() -> None:
    faults = [fault("F-001", KRI, "1004", "data_entry_lag")]
    findings = [
        finding("A", KRI, "1004", "data_entry_lag", 1.5, seq=1),
        finding("B", KRI, "1004", "data_entry_lag", 2.0, seq=2),
        finding("C", KRI, "1009", "data_entry_lag", 3.0, seq=3),
    ]
    result = match_faults(faults, findings)
    (m,) = result.matches
    assert m.finding_id == "B"
    assert m.duplicates == ("A",)
    assert result.true_positives == ("B",)
    assert set(result.false_positives) == {"A", "C"}


def test_kri_must_match_for_kri_breach() -> None:
    faults = [fault("F-001", KRI, "1004", "data_entry_lag")]
    result = match_faults(faults, [finding("A", KRI, "1004", "query_aging", 9.0)])
    assert not result.matches[0].detected
    assert result.false_positives == ("A",)


def test_kri_is_not_required_for_other_kinds() -> None:
    faults = [fault("F-001", FindingKind.SITE_DATA_ANOMALY, "1001", None)]
    result = match_faults(
        faults, [finding("A", FindingKind.SITE_DATA_ANOMALY, "1001", "variance", 4.0)]
    )
    assert result.matches[0].finding_id == "A"


def test_wrong_site_or_kind_does_not_match() -> None:
    faults = [fault("F-001", FindingKind.UNDER_REPORTING, "1010", "ae_rate")]
    findings = [
        finding("A", FindingKind.UNDER_REPORTING, "1005", "ae_rate", 5.0),
        finding("B", KRI, "1010", "ae_rate", 5.0),
    ]
    result = match_faults(faults, findings)
    assert not result.matches[0].detected
    assert set(result.false_positives) == {"A", "B"}


def test_study_level_fault_matches_study_finding() -> None:
    faults = [fault("F-006", FindingKind.QTL_BREACH, "STUDY", None)]
    result = match_faults(faults, [finding("Q", FindingKind.QTL_BREACH, "STUDY", "x", 1.1)])
    assert result.matches[0].finding_id == "Q"


def test_a_finding_matches_at_most_one_fault() -> None:
    faults = [
        fault("F-001", KRI, "1004", "data_entry_lag"),
        fault("F-002", KRI, "1004", "data_entry_lag"),
    ]
    result = match_faults(faults, [finding("A", KRI, "1004", "data_entry_lag", 2.0)])
    assert [m.finding_id for m in result.matches] == ["A", None]


def test_version_filter() -> None:
    registered = VersionFilter.registered({"rbqm.csm": "2.0.0", "rbqm.kri_monitor": "2.0.0"})
    assert registered.accepts("rbqm.csm", "2.0.0")
    assert not registered.accepts("rbqm.csm", "1.0.0")
    assert not registered.accepts("rbqm.other", "2.0.0")
    only_v1 = VersionFilter.only("1.0.0")
    assert only_v1.accepts("rbqm.anything", "1.0.0")
    assert not only_v1.accepts("rbqm.csm", "2.0.0")


def test_average_precision_ranks_by_score() -> None:
    findings = [
        finding("FP", KRI, "1", "x", 9.0, seq=1),
        finding("TP1", KRI, "2", "x", 5.0, seq=2),
        finding("TP2", KRI, "3", "x", 1.0, seq=3),
    ]
    # ranks: FP, TP1 (precision 1/2), TP2 (precision 2/3); 3 faults, one missed.
    assert average_precision(findings, {"TP1", "TP2"}, 3) == pytest.approx((0.5 + 2 / 3) / 3)
    assert average_precision(findings, set(), 0) is None


def test_cut_metrics_per_agent_and_type() -> None:
    faults = [
        fault("F-001", KRI, "1004", "data_entry_lag", "entry_lag_spike"),
        fault("F-002", FindingKind.SITE_DATA_ANOMALY, "1001", None, "fabricated_vitals"),
    ]
    findings = [
        finding("A", KRI, "1004", "data_entry_lag", 2.0),
        finding("B", KRI, "1009", "deviations", 3.0),
    ]
    result = match_faults(faults, findings)
    m = cut_metrics(faults, findings, result, ["rbqm.csm", "rbqm.kri_monitor"])
    assert m.overall.recall == 0.5
    assert m.overall.false_positives == 1
    assert m.overall.precision == 0.5
    assert m.per_agent["rbqm.kri_monitor"].recall == 1.0
    assert m.per_agent["rbqm.csm"].recall == 0.0  # the missed CSM fault is attributed to csm
    assert m.per_fault_type["fabricated_vitals"].recall == 0.0
    assert m.per_fault_type["entry_lag_spike"].recall == 1.0


def test_scorecard_is_written_once(tmp_path: Path) -> None:
    store = ScorecardStore(tmp_path / "evals")
    result = match_faults([], [])
    evaluation = CutEvaluation(
        cut_id="C",
        mode="null",
        metrics=cut_metrics([], [], result, []),
        matches=(),
        findings=(),
        false_positive_ids=(),
    )
    card = new_scorecard(
        clock=FixedClock(NOW),
        evaluations=[evaluation],
        versions=VersionFilter.only("2.0.0"),
        agents_config={"a": 1},
        git_sha="abc",
    )
    path = store.write(card)
    before = path.read_bytes()
    with pytest.raises(ScorecardExistsError):
        store.write(card)
    assert path.read_bytes() == before
    assert store.latest("C", "all agents at 2.0.0") == evaluation
    assert store.latest("C", "all agents at 1.0.0") is None
    other = new_scorecard(
        clock=FixedClock(NOW),
        evaluations=[evaluation],
        versions=VersionFilter.only("2.0.0"),
        agents_config={"a": 1},
        git_sha="abc",
    )
    assert other.run_id != card.run_id
    assert other.config_hash == card.config_hash
