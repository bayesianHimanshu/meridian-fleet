"""meridian eval suite end to end on the real pilot data. Needs network: run with -m slow.

The full step 5 sequence: base cut, rbqm_core, then the suite (nulls, holdout, sweep,
holdout 2, AE holdouts, agents, scorecards, report). The detection criteria are reported
in the PR, not asserted; rule agreement with condition truth must be 100% on every cut.
"""

import shutil
from pathlib import Path

import httpx
import pytest
import structlog

from meridian.data.cdisc_pilot import SDTM_DOMAINS
from meridian.data.fetch import fetch
from meridian.domain.enums import EventType
from meridian.evals.report import ae_frozen_commit
from meridian.evals.scorecard import freeze_order_violations
from meridian.faults.scenario import sweep_items
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.platform.eval_suite import run_suite, step5_cuts
from tests.support import T0

pytestmark = pytest.mark.slow
REPO_SCENARIOS = Path(__file__).resolve().parents[2] / "config" / "scenarios"


def test_eval_suite_on_real_data(tmp_path: Path) -> None:
    settings = Settings(
        ledger_path=tmp_path / "ledger.sqlite3",
        raw_dir=tmp_path / "raw",
        curated_root=tmp_path / "curated",
        sites_file=tmp_path / "sites.toml",
        ground_truth_dir=tmp_path / "ground_truth",
        evals_dir=tmp_path / "evals",
    )
    clock = FixedClock(T0)
    with httpx.Client(follow_redirects=True, timeout=settings.pilot.timeout_seconds) as client:
        fetch(
            client=client,
            clock=clock,
            base_url=settings.pilot.base_url,
            domains=SDTM_DOMAINS,
            raw_dir=settings.raw_dir,
        )
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    shutil.copy(REPO_SCENARIOS / "rbqm_core.toml", scenarios / "rbqm_core.toml")

    ae_sha = ae_frozen_commit()
    assert len(ae_sha) == 40, "the AE freeze commit must exist before holdout 2"
    with Container.build(settings, clock=clock) as c:
        c.ledger.initialize()
        c.ingest_service().ingest_raw(
            settings.raw_dir, cut_id="DC-BASE", seed=settings.default_seed
        )
        c.fault_injector().inject(scenarios / "rbqm_core.toml")
        result = run_suite(
            c,
            structlog.get_logger(),
            scenario_dir=scenarios,
            report_path=tmp_path / "eval-phase1.md",
        )
        assert result.agent_failures == 0
        assert verify(c.ledger).ok
        cuts = {e.payload.cut_id for e in c.ledger.read(event_types=[EventType.DATA_CUT])}  # type: ignore[union-attr]
        assert len(cuts) == 2 + 5 + 1 + len(sweep_items()) + 1 + 3
        holdout2, ae_holdouts = step5_cuts(scenarios)
        assert holdout2 is not None
        assert len(ae_holdouts) == 3
        (latest, *_) = sorted(
            (card for card in c.scorecard_store().all() if holdout2[0] in card.cuts),
            key=lambda card: card.created_at,
            reverse=True,
        )
        assert len(latest.cuts) == len(cuts)
        for evaluation in latest.evaluations:
            assert evaluation.rules is not None
            assert evaluation.rules.agreeing == evaluation.rules.units, evaluation.cut_id
        assert not freeze_order_violations(
            c.scorecard_store().all(), [holdout2[0], *(cut for cut, _ in ae_holdouts)], ae_sha
        )
        again = run_suite(
            c,
            structlog.get_logger(),
            scenario_dir=scenarios,
            report_path=tmp_path / "eval-phase1.md",
        )
        assert again.agent_runs == 0
        assert again.injected == []

    # Generated from DC-BASE again, the holdout and sweep files match the committed ones.
    assert (scenarios / "rbqm_holdout.toml").read_text() == (
        REPO_SCENARIOS / "rbqm_holdout.toml"
    ).read_text()
    for item in sweep_items():
        assert (scenarios / "sweep" / item.file_name).read_text() == (
            REPO_SCENARIOS / "sweep" / item.file_name
        ).read_text()
    # Holdout 2 and the AE holdouts too, byte for byte, so the opaque cut ids agree.
    for name in [
        "rbqm_holdout2.toml",
        "ae_holdout/1.toml",
        "ae_holdout/2.toml",
        "ae_holdout/3.toml",
    ]:
        if (REPO_SCENARIOS / name).exists():
            assert (scenarios / name).read_bytes() == (REPO_SCENARIOS / name).read_bytes(), name
    text = (tmp_path / "eval-phase1.md").read_text()
    assert "## 1. Exit criteria" in text
    assert chr(0x2014) not in text
