from datetime import UTC, datetime
from pathlib import Path

import meridian.evals.report as report
from meridian.data.study_store import ParquetStudyReader
from meridian.domain.enums import FindingKind
from meridian.evals.matching import VersionFilter, match_faults
from meridian.evals.metrics import cut_metrics, metrics
from meridian.evals.rules import RuleCondition, evaluate_rules
from meridian.evals.scorecard import (
    CutEvaluation,
    Scorecard,
    ScorecardStore,
    freeze_order_violations,
    new_scorecard,
)
from meridian.platform.agent_settings import CsmConfig, KriMonitorConfig, QtlDetectorConfig
from meridian.platform.clock import FixedClock
from tests.rbqm_support import make_tables, write_tables_cut
from tests.unit.test_evals import finding

AGENTS = ["rbqm.csm", "rbqm.kri_monitor", "rbqm.qtl_detector"]
V21 = VersionFilter.registered(
    {"rbqm.csm": "2.0.0", "rbqm.kri_monitor": "2.1.0", "rbqm.qtl_detector": "2.0.0"}
)
NOW = datetime(2026, 11, 7, tzinfo=UTC)


def _inputs(tmp_path: Path, store: ScorecardStore) -> report.ReportInputs:
    study = write_tables_cut(tmp_path / "curated", "DC-BASE", *make_tables(n_sites=12))
    return report.ReportInputs(
        store=store,
        v2_label=V21.label(),
        agents=AGENTS,
        base_study=ParquetStudyReader(study, "DC-BASE"),
        ground_truth_dir=tmp_path / "gt",
        kri=KriMonitorConfig(),
        qtl=QtlDetectorConfig(discontinuation_limit=0.617),
        csm=CsmConfig(),
        ledger_ok=True,
        ledger_events=7,
        frozen_sha="abc",
        head_sha="def",
        ae_frozen_sha="fed",
    )


def test_report_renders_with_no_scorecards(tmp_path: Path) -> None:
    text = report.render(_inputs(tmp_path, ScorecardStore(tmp_path / "evals")))
    assert "| Recall on DC-RBQM-CORE >= 0.8 | n/a | n/a |" in text
    assert "| Ledger verifies | | 7 events (pass) |" in text
    assert report.REVISION_NOTE in text
    assert "## 10. Sites on DC-BASE" in text
    assert "Thresholds frozen in commit `abc`" in text
    assert "the AE variant in commit `fed`" in text
    assert chr(0x2014) not in text


def _clean_evaluation(cut: str, natural: bool) -> CutEvaluation:
    empty = match_faults([], [])
    conditions = [
        RuleCondition(
            check="data_entry_lag",
            site="1003",
            value=7.4 if natural else 3.0,
            n=40,
            threshold=7.0,
            met=natural,
        )
    ]
    findings = (
        [finding("L", FindingKind.KRI_BREACH, "1003", "data_entry_lag", 1.06, version="2.1.0")]
        if natural
        else []
    )
    return CutEvaluation(
        cut_id=cut,
        mode="null",
        metrics=cut_metrics([], findings, match_faults([], findings), AGENTS),
        matches=(),
        findings=tuple(findings),
        false_positive_ids=tuple(f.finding_id for f in findings),
        scoring="rules_and_detectors",
        detector_metrics=metrics([], empty.matches, set()),
        condition_truth=tuple(conditions),
        rules=evaluate_rules(conditions, findings, []),
    )


def test_natural_breaches_count_in_the_original_null_criterion_not_the_revised(
    tmp_path: Path,
) -> None:
    store = ScorecardStore(tmp_path / "evals")
    cuts = ["DC-BASE", *(c for c, _ in report.NULL_CUTS)]
    evaluations = [_clean_evaluation(cut, natural=cut != "DC-BASE") for cut in cuts]
    store.write(
        new_scorecard(
            clock=FixedClock(NOW),
            evaluations=evaluations,
            versions=V21,
            agents_config={},
            git_sha="x",
        )
    )
    text = report.render(_inputs(tmp_path, store))
    assert "| Mean false positives per null cut <= 1 | n/a | 1.00 (pass) |" in text
    assert "| Mean detector false positives per null cut <= 1 | n/a | 0.00 (pass) |" in text
    assert "* DC-NULL-S1: data_entry_lag at 1003 (7.40 vs 7.00)" in text
    assert "* DC-BASE: none" in text


def test_freeze_order_violations_by_recorded_commit() -> None:
    def card(run: str, sha: str, cuts: list[str]) -> Scorecard:
        return new_scorecard(
            clock=FixedClock(NOW),
            evaluations=[_clean_evaluation(c, natural=False) for c in cuts],
            versions=V21,
            agents_config={},
            git_sha=sha,
        ).model_copy(update={"run_id": run})

    history = {"freeze": {"freeze"}, "after": {"freeze", "after"}, "before": {"before"}}

    def ancestry(ancestor: str, commit: str) -> bool:
        return ancestor in history.get(commit, set())

    cards = [
        card("r1", "before", ["DC-BASE"]),  # no holdout 2 cut: fine at any commit
        card("r2", "after", ["DC-BASE", "DC-abcdef12"]),
        card("r3", "before", ["DC-abcdef12"]),
    ]
    assert freeze_order_violations(cards, ["DC-abcdef12"], "freeze", ancestry) == ["r3"]
