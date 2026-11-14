"""Park and resume's workloads on kind (phase 4b step 4): deploy/gke/workloads.sh stops
every Substrate and Meridian workload, so a node drain has nothing to wait for, and starts
them again with Substrate's state intact. Run with -m substrate (CI has no cluster)."""

import json
import subprocess
from pathlib import Path

import pytest
import structlog

from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.sandbox.verify import trigger_for
from tests.substrate.test_cluster import _skip_reason

pytestmark = pytest.mark.substrate
SCRIPT = Path("deploy/gke/workloads.sh")


@pytest.fixture(scope="module", autouse=True)
def _cluster() -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)


def _kubectl(*args: str) -> str:
    s = Settings().substrate
    return subprocess.run(
        [s.kubectl, "--context", s.context, *args], capture_output=True, text=True, check=True
    ).stdout


def _state() -> dict[str, object]:
    """What must survive: every claim (uid, volume) and the atespace's tags."""
    s = Settings().substrate
    claims = json.loads(_kubectl("-n", "ate-system", "get", "pvc", "-o", "json"))["items"]
    tags = subprocess.run(
        [s.kubectl, "ate", "--context", s.context, "get", "tag", "-a", s.atespace, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {
        "claims": sorted((c["metadata"]["uid"], c["spec"]["volumeName"]) for c in claims),
        "tags": sorted(t["metadata"]["uid"] for t in json.loads(tags).get("tags", [])),
    }


def _workloads(action: str) -> str:
    context = Settings().substrate.context
    done = subprocess.run(
        [str(SCRIPT), "--context", context, action], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def _pods_not_daemonsets() -> list[str]:
    out = []
    for ns in ("ate-system", "meridian-dispatch", "meridian"):
        for p in json.loads(_kubectl("-n", ns, "get", "pods", "-o", "json"))["items"]:
            owners = [o["kind"] for o in p["metadata"].get("ownerReferences", [])]
            if "DaemonSet" not in owners and p["status"].get("phase") not in (
                "Succeeded",
                "Failed",
            ):
                out.append(f"{ns}/{p['metadata']['name']}")
    return out


def test_stop_leaves_only_daemonsets_and_start_brings_substrate_back_intact() -> None:
    before = _state()
    status = _workloads("status")
    assert "0 of" in status
    try:
        stopped = _workloads("stop")
        assert "no pod left but DaemonSets'" in stopped
        assert _pods_not_daemonsets() == []
        parked = _workloads("status")
        total = parked.strip().splitlines()[-1].split()
        assert total[0] == total[2]  # "N of N parked"
    finally:
        _workloads("start")
    assert "0 of" in _workloads("status")
    annotations = _kubectl(
        "-n", "ate-system", "get", "deploy,sts", "-o",
        "jsonpath={.items[*].metadata.annotations.meridian\\.dev/parked-replicas}",
    )  # fmt: skip
    assert annotations.strip() == ""
    assert _state() == before
    # And a run goes through the dispatcher again.
    with Container.build(Settings()) as c:
        executor = c.substrate_executor(structlog.get_logger())
        trigger = trigger_for(c.ledger, c.settings.study_id, "DC-BASE")
        result = executor.run("rbqm.kri_monitor", trigger, "DC-BASE")
        executor.drain()
        assert result.substrate is not None
