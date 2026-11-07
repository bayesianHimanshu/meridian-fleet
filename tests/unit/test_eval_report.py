from datetime import UTC, datetime
from pathlib import Path

from meridian.data.study_store import ParquetStudyReader
from meridian.evals import report
from meridian.evals.matching import VersionFilter, match_faults
from meridian.evals.metrics import cut_metrics
from meridian.evals.scorecard import CutEvaluation, ScorecardStore, new_scorecard
from meridian.platform.agent_settings import CsmConfig, KriMonitorConfig, QtlDetectorConfig
from meridian.platform.clock import FixedClock
from tests.rbqm_support import make_tables, write_tables_cut

AGENTS = ["rbqm.csm", "rbqm.kri_monitor", "rbqm.qtl_detector"]
V2 = VersionFilter.registered(dict.fromkeys(AGENTS, "2.0.0"))


def _inputs(tmp_path: Path, store: ScorecardStore) -> report.ReportInputs:
    study = write_tables_cut(tmp_path / "curated", "DC-BASE", *make_tables(n_sites=12))
    return report.ReportInputs(
        store=store,
        v2_label=V2.label(),
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
    )


def test_report_renders_with_no_scorecards(tmp_path: Path) -> None:
    text = report.render(_inputs(tmp_path, ScorecardStore(tmp_path / "evals")))
    assert "| Ledger verifies | 7 events | pass |" in text
    assert "| Recall on DC-RBQM-CORE >= 0.8 | n/a | not run |" in text
    assert "## 6. Sites on DC-BASE" in text
    assert "Frozen in commit `abc`" in text
    assert chr(0x2014) not in text


def test_report_uses_latest_null_scorecards(tmp_path: Path) -> None:
    store = ScorecardStore(tmp_path / "evals")
    empty = match_faults([], [])
    evaluations = [
        CutEvaluation(
            cut_id=cut,
            mode="null",
            metrics=cut_metrics([], [], empty, AGENTS),
            matches=(),
            findings=(),
            false_positive_ids=(),
        )
        for cut in ["DC-BASE", *(c for c, _ in report.NULL_CUTS)]
    ]
    store.write(
        new_scorecard(
            clock=FixedClock(datetime(2026, 11, 7, tzinfo=UTC)),
            evaluations=evaluations,
            versions=V2,
            agents_config={},
            git_sha="x",
        )
    )
    text = report.render(_inputs(tmp_path, store))
    assert "| False positives on DC-BASE <= 1 | 0 | pass |" in text
    assert "| Mean false positives on DC-NULL-S1 to S5 <= 1 per cut | 0.00 | pass |" in text
