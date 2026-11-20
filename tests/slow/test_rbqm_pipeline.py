"""Phase 1 step 3 end to end on the real pilot data. Needs network: run with -m slow.

Detection rates are deliberately not asserted here; step 4 does that.
"""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import structlog

from meridian.data.cdisc_pilot import SDTM_DOMAINS
from meridian.data.derived import premature_discontinuation
from meridian.data.fetch import fetch
from meridian.domain.enums import EventType
from meridian.faults.reader import FullCutReader
from meridian.faults.scenario import generate_rbqm_core
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.platform.container import Container
from tests.support import T0

pytestmark = pytest.mark.slow
REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO = REPO_ROOT / "config" / "scenarios" / "rbqm_core.toml"


def test_inject_run_agents_and_report_on_real_data(tmp_path: Path) -> None:
    paths = {
        "ledger_path": tmp_path / "var" / "ledger.sqlite3",
        "raw_dir": tmp_path / "raw",
        "curated_root": tmp_path / "curated",
        "sites_file": tmp_path / "sites.toml",
        "ground_truth_dir": tmp_path / "ground_truth",
    }
    settings = Settings(**paths)  # type: ignore[arg-type]
    clock = FixedClock(T0)
    with httpx.Client(follow_redirects=True, timeout=settings.pilot.timeout_seconds) as client:
        fetch(
            client=client,
            clock=clock,
            base_url=settings.pilot.base_url,
            domains=SDTM_DOMAINS,
            raw_dir=settings.raw_dir,
        )

    with Container.build(settings, clock=clock) as c:
        c.ledger.initialize()
        c.ingest_service().ingest_raw(
            settings.raw_dir, cut_id="DC-BASE", seed=settings.default_seed
        )

        base = FullCutReader(settings.curated_root / "MRD-201" / "DC-BASE").read()
        # The committed scenario is exactly what the generator produces from DC-BASE.
        assert generate_rbqm_core(base) == SCENARIO.read_text()
        # The committed QTL limit is the DC-BASE rate plus the 0.05 margin.
        discontinued, treated = premature_discontinuation(base.blinded["dm"], base.blinded["ds"])
        assert settings.agents.qtl_detector.discontinuation_limit == round(
            discontinued / treated + 0.05, 3
        )

        injected = c.fault_injector().inject(SCENARIO)
        assert injected.cut_created
        report = c.agent_runner(structlog.get_logger()).run_pending()
        assert report.failed == []
        # Every enabled DataCut agent on both cuts (3 rbqm agents in phase 1; with
        # safety.sae_reconciler and quality.tmf_completeness since phase 3, 5).
        on_cuts = [
            r for r in c.agent_registry().enabled() if EventType.DATA_CUT in r.agent.subscribes_to
        ]
        assert len(on_cuts) == 5
        assert len(report.completed) == 2 * len(on_cuts)
        again = c.agent_runner(structlog.get_logger()).run_pending()
        assert again.completed == []
        assert len(c.ledger.read(event_types=[EventType.DATA_CUT])) == 2
        assert verify(c.ledger).ok

    out = tmp_path / "calibration.md"
    env = {**os.environ, **{f"MERIDIAN_{k.upper()}": str(v) for k, v in paths.items()}}
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "calibration_report.py"), "--out", str(out)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    text = out.read_text()
    assert "## Planted faults" in text
    assert "## Sites on DC-BASE" in text
    assert chr(0x2014) not in text
