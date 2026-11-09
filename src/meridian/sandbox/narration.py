"""The narrator's OpenShell executor: one sandbox per pass, with the provider attached.

The host stages job.json (the requests it built) and an inputs manifest into /stage/narr
(the only staged area the narrator's policy grants), creates a sandbox
from the agent image with policies/rbqm.narrator.yaml and --provider meridian-anthropic,
runs meridian-narrator-worker, checks the effective policy, and always deletes the
sandbox. The sandbox only carries the calls: the host built every request and validates
every response.
"""

import tempfile
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import structlog
from pydantic import ValidationError

from meridian.agents.narration import NarrationBatch
from meridian.data.inputs_manifest import write_manifest
from meridian.llm.client import ModelRequest
from meridian.llm.job import JOB_DIR, JOB_FILE, NarrationJob, NarrationResults
from meridian.sandbox.executor import STDERR_TAIL, ExecutionError
from meridian.sandbox.openshell import OpenShellClient, OpenShellError
from meridian.sandbox.policies import (
    ALL_POLICIES,
    NARRATOR_POLICY,
    expected_effective,
    policy_path,
    sha256_text,
    stage_path,
)
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker

OPENSHELL = "openshell"
NARRATOR_WORKER = "/opt/meridian/venv/bin/meridian-narrator-worker"
SANDBOX_JOB = f"{stage_path(JOB_DIR)}/{JOB_FILE}"


def job_uploads(stage: Path) -> list[tuple[Path, str]]:
    """Every file of a job folder, uploaded straight into /stage/<folder name>."""
    return [(f, stage_path(stage.name)) for f in sorted(stage.iterdir()) if f.is_file()]


def narrator_sandbox_name() -> str:
    return f"mer-nr-{uuid.uuid4().hex[:8]}"


class OpenShellNarrationExecutor:
    name = OPENSHELL

    def __init__(
        self,
        *,
        client: OpenShellClient,
        image: str,
        policies_dir: Path,
        tracker: SandboxTracker,
        deleter: BackgroundDeleter,
        log: structlog.typing.FilteringBoundLogger,
        staging_dir: Path | None = None,
        name_factory: Callable[[], str] = narrator_sandbox_name,
    ) -> None:
        self._client = client
        self._image = image
        self._policies_dir = policies_dir
        self._tracker = tracker
        self._deleter = deleter
        self._log = log
        self._staging_dir = staging_dir
        self._name_factory = name_factory

    def call(self, requests: Sequence[ModelRequest], timeout_seconds: float) -> NarrationBatch:
        policy = ALL_POLICIES[NARRATOR_POLICY]
        policy_file = policy_path(NARRATOR_POLICY, self._policies_dir)
        name = self._name_factory()
        log = self._log.bind(sandbox=name, requests=len(requests))
        # Each request may be retried once; the slack covers start-up.
        exec_timeout = int(len(requests) * (2 * timeout_seconds + 5) + 60)
        with tempfile.TemporaryDirectory(prefix="meridian-narr-", dir=self._staging_dir) as tmp:
            stage = Path(tmp) / JOB_DIR
            stage.mkdir()
            job = NarrationJob(requests=tuple(requests))
            (stage / JOB_FILE).write_text(job.model_dump_json(), encoding="utf-8")
            write_manifest(stage)
            self._tracker.record(name, "created")
            try:
                self._client.create(
                    name,
                    self._image,
                    policy_file,
                    job_uploads(stage),
                    providers=policy.providers,
                )
                effective = self._client.policy(name)
                if effective.strip() != expected_effective(policy).strip():
                    raise ExecutionError(
                        f"sandbox {name} reports a policy that differs from {policy_file}"
                    )
                out = self._client.exec(
                    name,
                    [NARRATOR_WORKER, "--job", SANDBOX_JOB, "--timeout", str(timeout_seconds)],
                    exec_timeout,
                )
                if out.exit_code != 0:
                    raise ExecutionError(
                        f"meridian-narrator-worker exited {out.exit_code} in {name}: "
                        f"{out.stderr[-STDERR_TAIL:]}"
                    )
                try:
                    results = NarrationResults.model_validate_json(out.stdout)
                except (ValueError, ValidationError) as exc:
                    raise ExecutionError(f"worker stdout is not narration results: {exc}") from exc
            except OpenShellError as exc:
                raise ExecutionError(f"openshell failed for {name}: {exc}") from exc
            finally:
                self._deleter.submit(name)
        log.info("narrator.sandbox_done", results=len(results.results))
        return NarrationBatch(
            results=results.results,
            executor=self.name,
            sandbox=name,
            policy_sha256=sha256_text(effective),
        )

    def drain(self) -> None:
        report = self._deleter.drain()
        if report.failed:
            self._log.error("sandbox.deletes_failed", sandboxes=report.failed)
