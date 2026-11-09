"""OpenShellNarrationExecutor against a fake OpenShell client."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import structlog

from meridian.data.inputs_manifest import verify_manifest
from meridian.ledger.hashing import canonical_json
from meridian.llm.client import ModelRequest
from meridian.llm.job import JOB_FILE, CallResult, NarrationJob, NarrationResults
from meridian.platform.clock import FixedClock
from meridian.sandbox import policies
from meridian.sandbox.executor import ExecutionError
from meridian.sandbox.narration import NARRATOR_WORKER, SANDBOX_JOB, OpenShellNarrationExecutor
from meridian.sandbox.openshell import ExecOutput
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker
from tests.support import T0

REPO = Path(__file__).resolve().parents[2]
REQUESTS = (
    ModelRequest(model="m", system="s", user="u1", max_tokens=300, temperature=0.0),
    ModelRequest(model="m", system="s", user="u2", max_tokens=300, temperature=0.0),
)


@dataclass
class FakeClient:
    effective: str = policies.expected_effective(policies.ALL_POLICIES["rbqm.narrator"])
    exit_code: int = 0
    live: set[str] = field(default_factory=set)
    created: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    staged: dict[str, str] = field(default_factory=dict)
    argv: list[str] = field(default_factory=list)
    timeout: int = 0
    deleted: list[str] = field(default_factory=list)

    def create(
        self,
        name: str,
        image: str,
        policy: Path,
        uploads: Sequence[tuple[Path, str]],
        providers: Sequence[str] = (),
    ) -> None:
        assert policy.name == "rbqm.narrator.yaml"
        # Each file straight into /stage/narr, the only area the narrator's policy grants.
        assert {remote for _, remote in uploads} == {"/stage/narr"}
        (folder,) = {local.parent for local, _ in uploads}
        assert sorted(local for local, _ in uploads) == sorted(folder.iterdir())
        verify_manifest(folder)  # what the worker checks first
        self.staged = {local.name: local.read_text() for local, _ in uploads}
        self.created.append((name, tuple(providers)))
        self.live.add(name)

    def exec(self, name: str, argv: Sequence[str], timeout_seconds: int) -> ExecOutput:
        self.argv, self.timeout = list(argv), timeout_seconds
        job = NarrationJob.model_validate_json(self.staged[JOB_FILE])
        results = NarrationResults(
            results=tuple(
                CallResult(request_sha256=r.sha256(), error_kind="transport", error="x")
                for r in job.requests
            )
        )
        return ExecOutput(self.exit_code, canonical_json(results.model_dump(mode="json")), "e")

    def policy(self, name: str) -> str:
        return self.effective

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.live.discard(name)

    def names(self) -> list[str]:
        return sorted(self.live)


def _executor(client: FakeClient, tmp_path: Path) -> OpenShellNarrationExecutor:
    tracker = SandboxTracker(tmp_path / "sandboxes.jsonl", FixedClock(T0))
    log = structlog.get_logger()
    return OpenShellNarrationExecutor(
        client=client,
        image="meridian-agent:test",
        policies_dir=REPO / "policies",
        tracker=tracker,
        deleter=BackgroundDeleter(
            delete=client.delete, tracker=tracker, workers=0, log=log, live=client.names
        ),
        log=log,
        name_factory=lambda: "mer-nr-00000000",
    )


def test_one_sandbox_with_the_provider_carries_every_request(tmp_path: Path) -> None:
    client = FakeClient()
    batch = _executor(client, tmp_path).call(REQUESTS, timeout_seconds=30.0)
    assert client.created == [("mer-nr-00000000", ("meridian-anthropic",))]
    assert sorted(client.staged) == ["inputs_manifest.json", "job.json"]
    staged = json.loads(client.staged[JOB_FILE])
    assert [r["user"] for r in staged["requests"]] == ["u1", "u2"]
    assert client.argv == [NARRATOR_WORKER, "--job", SANDBOX_JOB, "--timeout", "30.0"]
    assert client.timeout == 2 * (2 * 30 + 5) + 60
    assert [r.request_sha256 for r in batch.results] == [r.sha256() for r in REQUESTS]
    assert (batch.executor, batch.sandbox) == ("openshell", "mer-nr-00000000")
    assert batch.policy_sha256 == policies.sha256_text(client.effective)
    assert client.deleted == ["mer-nr-00000000"]


def test_a_different_effective_policy_stops_the_batch(tmp_path: Path) -> None:
    client = FakeClient(effective="version: 1")
    with pytest.raises(ExecutionError, match="differs"):
        _executor(client, tmp_path).call(REQUESTS, timeout_seconds=30.0)
    assert client.argv == []  # nothing ran
    assert client.deleted == ["mer-nr-00000000"]


def test_a_failed_worker_stops_the_batch_and_still_deletes(tmp_path: Path) -> None:
    client = FakeClient(exit_code=3)
    with pytest.raises(ExecutionError, match="exited 3"):
        _executor(client, tmp_path).call(REQUESTS, timeout_seconds=30.0)
    assert client.deleted == ["mer-nr-00000000"]
