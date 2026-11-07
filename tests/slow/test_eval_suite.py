"""meridian eval suite end to end on the real pilot data. Needs network: run with -m slow.

The exit criteria are deliberately not asserted here; they are reported in the PR.
"""

import shutil
from pathlib import Path

import httpx
import pytest
import structlog

from meridian.data.cdisc_pilot import SDTM_DOMAINS
from meridian.data.fetch import fetch
from meridian.domain.enums import EventType
from meridian.faults.scenario import sweep_items
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.platform.eval_suite import run_suite
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
        assert len(cuts) == 2 + 5 + 1 + len(sweep_items())
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
    text = (tmp_path / "eval-phase1.md").read_text()
    assert "## 1. Exit criteria" in text
    assert chr(0x2014) not in text
