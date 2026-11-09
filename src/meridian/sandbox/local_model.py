"""The local model's OpenShell executor: one sandbox per pass, no provider.

The host stages job.json (the requests it built, the model and its pinned digest) and an
inputs manifest into /stage/lm (the only staged area policies/safety.local_model.yaml
grants), creates a sandbox from the agent image with that policy, checks the effective
policy, runs meridian-local-model-worker and always deletes the sandbox. The worker
checks the served digest before sending anything; the host validates every response.
"""

import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

import structlog
from pydantic import ValidationError

from meridian.agents.local_model import LocalBatch
from meridian.data.inputs_manifest import write_manifest
from meridian.llm.local_job import JOB_DIR, JOB_FILE, LocalModelJob, LocalModelResults
from meridian.sandbox.executor import STDERR_TAIL, ExecutionError
from meridian.sandbox.narration import job_uploads
from meridian.sandbox.openshell import OpenShellClient, OpenShellError
from meridian.sandbox.policies import (
    ALL_POLICIES,
    LOCAL_MODEL_POLICY,
    expected_effective,
    policy_path,
    sha256_text,
    stage_path,
)
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker

OPENSHELL = "openshell"
LOCAL_WORKER = "/opt/meridian/venv/bin/meridian-local-model-worker"
SANDBOX_JOB = f"{stage_path(JOB_DIR)}/{JOB_FILE}"


def local_sandbox_name() -> str:
    return f"mer-lm-{uuid.uuid4().hex[:8]}"


class OpenShellLocalModelExecutor:
    name = OPENSHELL

    def __init__(
        self,
        *,
        client: OpenShellClient,
        image: str,
        policies_dir: Path,
        base_url: str,
        tracker: SandboxTracker,
        deleter: BackgroundDeleter,
        log: structlog.typing.FilteringBoundLogger,
        staging_dir: Path | None = None,
        name_factory: Callable[[], str] = local_sandbox_name,
    ) -> None:
        self._client = client
        self._image = image
        self._policies_dir = policies_dir
        self._base_url = base_url
        self._tracker = tracker
        self._deleter = deleter
        self._log = log
        self._staging_dir = staging_dir
        self._name_factory = name_factory

    def call(self, job: LocalModelJob, timeout_seconds: float) -> LocalBatch:
        policy = ALL_POLICIES[LOCAL_MODEL_POLICY]
        policy_file = policy_path(LOCAL_MODEL_POLICY, self._policies_dir)
        name = self._name_factory()
        log = self._log.bind(sandbox=name, requests=len(job.requests), model=job.model)
        # Each request may be retried once; the slack covers start-up and the model load.
        exec_timeout = int(len(job.requests) * (2 * timeout_seconds + 5) + 120)
        with tempfile.TemporaryDirectory(prefix="meridian-lm-", dir=self._staging_dir) as tmp:
            stage = Path(tmp) / JOB_DIR
            stage.mkdir()
            (stage / JOB_FILE).write_text(job.model_dump_json(), encoding="utf-8")
            write_manifest(stage)
            self._tracker.record(name, "created")
            try:
                self._client.create(name, self._image, policy_file, job_uploads(stage))
                effective = self._client.policy(name)
                if effective.strip() != expected_effective(policy).strip():
                    raise ExecutionError(
                        f"sandbox {name} reports a policy that differs from {policy_file}"
                    )
                out = self._client.exec(
                    name,
                    [
                        LOCAL_WORKER,
                        "--job",
                        SANDBOX_JOB,
                        "--base-url",
                        self._base_url,
                        "--timeout",
                        str(timeout_seconds),
                    ],
                    exec_timeout,
                )
                if out.exit_code != 0:
                    raise ExecutionError(
                        f"meridian-local-model-worker exited {out.exit_code} in {name}: "
                        f"{out.stderr[-STDERR_TAIL:]}"
                    )
                try:
                    results = LocalModelResults.model_validate_json(out.stdout)
                except (ValueError, ValidationError) as exc:
                    raise ExecutionError(
                        f"worker stdout is not local model results: {exc}"
                    ) from exc
            except OpenShellError as exc:
                raise ExecutionError(f"openshell failed for {name}: {exc}") from exc
            finally:
                self._deleter.submit(name)
        log.info("local_model.sandbox_done", results=len(results.results))
        return LocalBatch(
            results=results.results,
            served_digest=results.served_digest,
            executor=self.name,
            sandbox=name,
            policy_sha256=sha256_text(effective),
        )

    def drain(self) -> None:
        report = self._deleter.drain()
        if report.failed:
            self._log.error("sandbox.deletes_failed", sandboxes=report.failed)
