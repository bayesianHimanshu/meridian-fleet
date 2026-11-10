"""quality.audit_trail_reviewer in a real sandbox under policies/quality.audit_trail.yaml.
Run with -m openshell.

The stage holds the trigger and the audit extract only; the drafts are byte for byte those
of an in-process run on the same extract.
"""

from pathlib import Path

import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.execute import drafts_document, run_agent
from meridian.agents.quality.audit_trail_reviewer import AGENT_ID
from meridian.audit.extract import extract_rows, write_extract
from meridian.data.study_store import AuditStageReader
from meridian.domain import audit as a
from meridian.domain.enums import DecisionAction, Role
from meridian.domain.events import AuditReviewRequested, HumanDecision
from meridian.domain.findings import Decision
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.sandbox import image, policies
from meridian.sandbox.executor import OpenShellExecutor
from meridian.sandbox.openshell import SubprocessOpenShellClient
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker
from tests.api_support import T0, build
from tests.openshell.test_sandbox import _skip_reason

pytestmark = pytest.mark.openshell
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _openshell_available() -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)


def test_the_reviewer_runs_sandboxed_under_its_policy_with_identical_drafts(tmp_path: Path) -> None:
    fx = build(tmp_path / "api", restricted=True)
    fx.append(
        HumanDecision(
            decision=Decision(
                finding_id="RBQM-0001",
                action=DecisionAction.DISMISSED,
                actor="asha.rao",
                role=Role.CLINICAL_OPS_LEAD,
                reason="ok",
            )
        )
    )
    through = fx.events[-1].seq
    trigger = fx.append(AuditReviewRequested(study_id="MRD-201", through_seq=through))
    rows = extract_rows(fx.events, from_seq=1, through_seq=through, operational_cuts=["DC-BASE"])
    write_extract(rows, tmp_path / "audit" / str(trigger.seq) / "audit")
    client = SubprocessOpenShellClient()
    registry = build_registry(Settings().agents)
    ref = image.default_image()
    tracker = SandboxTracker(tmp_path / "sandboxes.jsonl", FixedClock(T0))
    log = structlog.get_logger()
    executor = OpenShellExecutor(
        client=client,
        tracker=tracker,
        deleter=BackgroundDeleter(
            delete=client.delete, tracker=tracker, workers=2, log=log, live=client.names
        ),
        registry=registry,
        study_root=tmp_path / "no-cuts",
        image=ref,
        image_digest=image.inspect(ref).digest,
        policies_dir=REPO / "policies",
        exec_timeout_seconds=120,
        keep_failed=False,
        log=log,
        audit_root=tmp_path / "audit",
    )
    result = executor.run(AGENT_ID, trigger, a.LEDGER_SCOPE)
    executor.drain()
    registered = registry.get(AGENT_ID)
    reader = AuditStageReader.at_cut_dir(
        tmp_path / "audit" / str(trigger.seq), cut_id=a.LEDGER_SCOPE, study_id="MRD-201"
    )
    here = run_agent(registered, trigger, reader, log)
    assert {d.dedup_key.split("|")[5] for d in here} == {f"audit_reason_quality:{through}"}
    assert drafts_document(registered, result.drafts) == drafts_document(registered, here)
    expected = policies.expected_effective(policies.ALL_POLICIES["quality.audit_trail"])
    assert result.policy_sha256 == policies.sha256_text(expected)
    assert result.policy_file == "policies/quality.audit_trail.yaml"
    assert tracker.pending() == []
