"""meridian sandbox conformance for every zone, in real sandboxes. Run with -m openshell."""

from pathlib import Path

import pytest
import structlog

from meridian.domain.events import ContainmentCheckCompleted, PolicyDenied
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.sandbox import image, policies
from meridian.sandbox.conformance import ledger_drafts, run_conformance
from meridian.sandbox.openshell import SubprocessOpenShellClient
from meridian.sandbox.tracking import SandboxTracker
from tests.openshell.test_sandbox import _skip_reason
from tests.rbqm_support import NOW
from tests.support import make_ledger

pytestmark = pytest.mark.openshell
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _openshell_available() -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)


@pytest.mark.parametrize("zone", sorted(policies.POLICIES))
def test_zone_policy_conforms_and_is_recorded(tmp_path: Path, zone: str) -> None:
    client = SubprocessOpenShellClient()
    ref = image.default_image()
    tracker = SandboxTracker(tmp_path / "sandboxes.jsonl", FixedClock(NOW))
    result = run_conformance(
        client=client,
        zone=zone,
        image=ref,
        image_digest=image.inspect(ref).digest,
        policies_dir=REPO / "policies",
        tracker=tracker,
        log=structlog.get_logger(),
        log_timeout_seconds=30,
    )
    assert result.passed, result.failures()
    assert tracker.pending() == []
    assert result.sandbox not in client.names()

    ledger = make_ledger(tmp_path / "ledger.sqlite3", FixedClock(NOW))
    events = [ledger.append(d) for d in ledger_drafts(result, FixedClock(NOW))]
    assert verify(ledger).ok
    ledger.close()
    sources = [
        (e.payload.check, e.payload.source) for e in events if isinstance(e.payload, PolicyDenied)
    ]
    assert sources == [
        ("read_outside_allow_list", "harness"),
        ("write_var_tmp", "harness"),
        ("write_opt_meridian", "harness"),
        ("egress_test_net", "openshell_log"),
    ]
    done = events[-1].payload
    assert isinstance(done, ContainmentCheckCompleted)
    assert done.all_refused
    assert done.passed
