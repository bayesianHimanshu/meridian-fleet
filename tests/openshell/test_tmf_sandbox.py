"""quality.tmf_completeness in a real sandbox under policies/quality.yaml. Run with -m openshell.

The stage holds the cut's blinded tables and, under /stage/tmf, its eTMF snapshot; the
drafts are byte for byte those of an in-process run.
"""

from pathlib import Path

import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.execute import drafts_document, run_agent
from meridian.agents.quality.tmf_completeness import AGENT_ID
from meridian.data.study_store import LayeredStudyReader, ParquetStudyReader
from meridian.domain import tmf as t
from meridian.faults.reader import FullCutReader
from meridian.faults.tmf import TmfFault, TmfScenario, plant
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.platform.tmf_reference import load_tmf_reference
from meridian.sandbox import image, policies
from meridian.sandbox.executor import OpenShellExecutor
from meridian.sandbox.openshell import SubprocessOpenShellClient
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker
from meridian.tmf.generate import write_snapshot
from tests.agent_support import trigger
from tests.openshell.test_sandbox import _skip_reason
from tests.rbqm_support import NOW, STUDY
from tests.tmf_support import tmf_base

pytestmark = pytest.mark.openshell
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _openshell_available() -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)


def test_the_tmf_agent_runs_sandboxed_under_the_quality_policy(tmp_path: Path) -> None:
    ref = load_tmf_reference()
    base = tmf_base(tmp_path, "DC-TMF1")
    scenario = TmfScenario(
        set_name="dev",
        index=1,
        seed=4,
        base_cut="DC-TMF1",
        faults=(
            TmfFault(type=t.MISSING_ARTIFACT, params={}),
            TmfFault(type=t.APPROVAL_AFTER_FIRST_SUBJECT, params={}),
        ),
    )
    planted = plant(scenario, base, ref)
    study = tmp_path / "curated" / STUDY
    write_snapshot(
        etmf_root=tmp_path / "etmf",
        study_id=STUDY,
        cut=FullCutReader(study / "DC-TMF1").read(),
        index=planted.index,
        seed=scenario.seed,
        clock=FixedClock(NOW),
    )
    client = SubprocessOpenShellClient()
    registry = build_registry(Settings().agents)
    ref_image = image.default_image()
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
        image=ref_image,
        image_digest=image.inspect(ref_image).digest,
        policies_dir=REPO / "policies",
        exec_timeout_seconds=120,
        keep_failed=False,
        log=log,
        etmf_root=tmp_path / "etmf" / STUDY,
    )
    result = executor.run(AGENT_ID, trigger("DC-TMF1"), "DC-TMF1")
    executor.drain()
    registered = registry.get(AGENT_ID)
    reader = LayeredStudyReader(
        ParquetStudyReader(study, "DC-TMF1"),
        ParquetStudyReader(tmp_path / "etmf" / STUDY, "DC-TMF1"),
    )
    here = run_agent(registered, trigger("DC-TMF1"), reader, log)
    assert {d.dedup_key.split("|")[5] for d in here} == {
        f"{r.kri}:{r.target}" for r in planted.records
    }
    assert drafts_document(registered, result.drafts) == drafts_document(registered, here)
    expected = policies.expected_effective(policies.ALL_POLICIES["quality"])
    assert result.policy_sha256 == policies.sha256_text(expected)
    assert result.policy_file == "policies/quality.yaml"
    assert tracker.pending() == []
