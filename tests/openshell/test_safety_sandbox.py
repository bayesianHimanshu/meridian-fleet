"""safety.sae_reconciler in a real sandbox under its own policy. Run with -m openshell.

The stage holds the cut's blinded and ops tables and the blinded case table of its safety
database snapshot; the drafts are byte for byte those of an in-process run.
"""

from pathlib import Path

import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.execute import drafts_document, run_agent
from meridian.agents.safety.sae_reconciler import AGENT_ID
from meridian.data.study_store import LayeredStudyReader, ParquetStudyReader
from meridian.domain import safety as s
from meridian.faults.safety import SafetyFault, SafetyScenario, plant
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.safetydb.generate import write_snapshot
from meridian.sandbox import image, policies
from meridian.sandbox.executor import OpenShellExecutor
from meridian.sandbox.openshell import SubprocessOpenShellClient
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker
from tests.agent_support import trigger
from tests.openshell.test_sandbox import _skip_reason
from tests.rbqm_support import NOW, STUDY, write_tables_cut
from tests.safety_support import base_cut

pytestmark = pytest.mark.openshell
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _openshell_available() -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)


def test_the_reconciler_runs_sandboxed_under_its_policy_with_identical_drafts(
    tmp_path: Path,
) -> None:
    base = base_cut(tmp_path / "base")
    scenario = SafetyScenario(
        set_name="dev",
        index=1,
        seed=4,
        base_cut="DC-BASE",
        augment=0,
        faults=(
            SafetyFault(type=s.OUTCOME_MISMATCH, params={}),
            SafetyFault(type=s.LATE_REPORT, params={"delay_hours": 60.0}),
        ),
    )
    planted = plant(scenario, base)
    cut_id = "DC-SAFE1"
    study = write_tables_cut(
        tmp_path / "curated", cut_id, {**base.blinded, "ae": planted.ae}, base.ops, base.unblinded
    )
    from meridian.faults.reader import FullCutReader

    cut = FullCutReader(study / cut_id).read()
    write_snapshot(
        safety_root=tmp_path / "safety",
        study_id=STUDY,
        cut=cut,
        cases=planted.cases,
        harmless=planted.harmless,
        seed=planted.case_seed,
        clock=FixedClock(NOW),
    )
    client = SubprocessOpenShellClient()
    registry = build_registry(Settings().agents)
    ref = image.default_image()
    tracker = SandboxTracker(tmp_path / "sandboxes.jsonl", FixedClock(NOW))
    log = structlog.get_logger()
    executor = OpenShellExecutor(
        client=client,
        tracker=tracker,
        deleter=BackgroundDeleter(
            delete=client.delete, tracker=tracker, workers=2, log=log, live=client.names
        ),
        registry=registry,
        study_root=study,
        image=ref,
        image_digest=image.inspect(ref).digest,
        policies_dir=REPO / "policies",
        exec_timeout_seconds=120,
        keep_failed=False,
        log=log,
        safety_root=tmp_path / "safety" / STUDY,
    )
    result = executor.run(AGENT_ID, trigger(cut_id), cut_id)
    executor.drain()
    registered = registry.get(AGENT_ID)
    reader = LayeredStudyReader(
        ParquetStudyReader(study, cut_id), ParquetStudyReader(tmp_path / "safety" / STUDY, cut_id)
    )
    here = run_agent(registered, trigger(cut_id), reader, log)
    assert len(here) == 2
    assert drafts_document(registered, result.drafts) == drafts_document(registered, here)
    expected = policies.expected_effective(policies.ALL_POLICIES[AGENT_ID])
    assert result.policy_sha256 == policies.sha256_text(expected)
    assert tracker.pending() == []
