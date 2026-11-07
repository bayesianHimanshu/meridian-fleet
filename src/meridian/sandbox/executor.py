"""Where an agent run happens: in process, or in a fresh OpenShell sandbox.

Either way the result is the same list of FindingDrafts; only the host runner writes to
the ledger. The OpenShell executor creates one sandbox per run from the agent image with
the zone's policy, uploads a staged copy of the cut (blinded/, ops/ and the trigger,
nothing else), runs meridian-worker, reads back the effective policy and always deletes
the sandbox.
"""

import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import ValidationError

from meridian.agents.base import FindingDraft, zone_of
from meridian.agents.execute import DraftsDocument, run_agent
from meridian.agents.registry import AgentRegistry
from meridian.data.inputs_manifest import MANIFEST_FILE, build_manifest
from meridian.data.study_store import StudyReader
from meridian.domain.events import DataCut, Event
from meridian.sandbox.openshell import OpenShellClient, OpenShellError
from meridian.sandbox.policies import POLICIES, expected_effective, policy_path, sha256_text
from meridian.sandbox.staging import (
    SANDBOX_CUT_DIR,
    SANDBOX_ROOT,
    SANDBOX_TRIGGER,
    assert_clean_stage,
    stage_cut,
)

INPROCESS = "inprocess"
OPENSHELL = "openshell"
WORKER = "/opt/meridian/venv/bin/meridian-worker"
STDERR_TAIL = 2000


class ExecutionError(Exception):
    """The run failed; the runner records nothing and retries on the next pass."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    drafts: tuple[FindingDraft, ...]
    executor: str
    image_digest: str | None
    policy_sha256: str | None
    duration_ms: int
    stderr_tail: str
    inputs_manifest_sha256: str | None = None


class Executor(Protocol):
    @property
    def name(self) -> str: ...

    def run(self, agent_id: str, trigger: Event, cut_id: str) -> ExecutionResult: ...


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


class InProcessExecutor:
    """Phase 1 behaviour: the agent runs in this process on a ParquetStudyReader."""

    name = INPROCESS

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        reader_factory: Callable[[str], StudyReader],
        log: structlog.typing.FilteringBoundLogger,
    ) -> None:
        self._registry = registry
        self._reader_factory = reader_factory
        self._log = log

    def run(self, agent_id: str, trigger: Event, cut_id: str) -> ExecutionResult:
        start = time.perf_counter()
        registered = self._registry.get(agent_id)
        log = self._log.bind(agent_id=agent_id, trigger_seq=trigger.seq, cut_id=cut_id)
        drafts = run_agent(registered, trigger, self._reader_factory(cut_id), log)
        return ExecutionResult(
            drafts=tuple(drafts),
            executor=self.name,
            image_digest=None,
            policy_sha256=None,
            duration_ms=_ms(start),
            stderr_tail="",
        )


# The installed gateway (0.1.7) refuses sandbox names longer than 19 characters.
MAX_SANDBOX_NAME = 19
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = _B36[r] + out
        if n == 0:
            return out


def agent_code(agent_id: str) -> str:
    """Two letters: initials of the agent name, or its first two letters if one word."""
    words = agent_id.split(".", 1)[1].split("_")
    return "".join(w[0] for w in words)[:2] if len(words) > 1 else words[0][:2]


def sandbox_name(agent_id: str, trigger_seq: int) -> str:
    """mer-<agent code>-<trigger seq in base 36>-<8 hex>, at most 19 characters.

    For example rbqm.kri_monitor on DataCut seq 275 gives mer-km-7n-1a2b3c4d. The seq is
    dropped if it would not fit (beyond seq 46655); the 8 hex keep names unique.
    """
    tail = uuid.uuid4().hex[:8]
    name = f"mer-{agent_code(agent_id)}-{_base36(trigger_seq)}-{tail}"
    return name if len(name) <= MAX_SANDBOX_NAME else f"mer-{agent_code(agent_id)}-{tail}"


class OpenShellExecutor:
    name = OPENSHELL

    def __init__(
        self,
        *,
        client: OpenShellClient,
        registry: AgentRegistry,
        study_root: Path,
        image: str,
        image_digest: str,
        policies_dir: Path,
        exec_timeout_seconds: int,
        keep_failed: bool,
        log: structlog.typing.FilteringBoundLogger,
        staging_dir: Path | None = None,
        name_factory: Callable[[str, int], str] = sandbox_name,
    ) -> None:
        self._client = client
        self._registry = registry
        self._study_root = study_root
        self._image = image
        self._image_digest = image_digest
        self._policies_dir = policies_dir
        self._timeout = exec_timeout_seconds
        self._keep_failed = keep_failed
        self._log = log
        self._staging_dir = staging_dir
        self._name_factory = name_factory

    def run(self, agent_id: str, trigger: Event, cut_id: str) -> ExecutionResult:
        start = time.perf_counter()
        registered = self._registry.get(agent_id)
        if not isinstance(trigger.payload, DataCut) or trigger.payload.cut_id != cut_id:
            raise ExecutionError(f"trigger {trigger.seq} is not the DataCut of {cut_id}")
        zone = zone_of(agent_id)
        policy = POLICIES[zone]
        policy_file = policy_path(zone, self._policies_dir)
        name = self._name_factory(agent_id, trigger.seq)
        log = self._log.bind(agent_id=agent_id, trigger_seq=trigger.seq, sandbox=name)
        with tempfile.TemporaryDirectory(prefix="meridian-stage-", dir=self._staging_dir) as tmp:
            stage = stage_cut(self._study_root / cut_id, trigger, Path(tmp))
            assert_clean_stage(stage)  # again, right before it leaves the host
            recomputed = build_manifest(stage)
            if (stage / MANIFEST_FILE).read_text(encoding="utf-8") != recomputed.dumps():
                raise ExecutionError("staged files changed after the inputs manifest was written")
            inputs_sha = recomputed.sha256()
            ok = False
            try:
                self._client.create(name, self._image, policy_file, [(stage, SANDBOX_ROOT)])
                out = self._client.exec(name, self._argv(agent_id), self._timeout)
                effective = self._client.policy(name)
                stderr_tail = out.stderr[-STDERR_TAIL:]
                if effective.strip() != expected_effective(policy).strip():
                    raise ExecutionError(
                        f"sandbox {name} reports a policy that differs from {policy_file}"
                    )
                if out.exit_code != 0:
                    raise ExecutionError(
                        f"meridian-worker exited {out.exit_code} in {name}: {stderr_tail}"
                    )
                drafts = self._parse(
                    out.stdout, registered.agent.agent_id, registered.agent.version
                )
                ok = True
            except OpenShellError as exc:
                raise ExecutionError(f"openshell failed for {name}: {exc}") from exc
            finally:
                self._finish(name, ok, log)
        result = ExecutionResult(
            drafts=drafts,
            executor=self.name,
            image_digest=self._image_digest,
            policy_sha256=sha256_text(effective),
            duration_ms=_ms(start),
            stderr_tail=stderr_tail,
            inputs_manifest_sha256=inputs_sha,
        )
        log.info("sandbox.run_done", duration_ms=result.duration_ms, drafts=len(drafts))
        return result

    def _argv(self, agent_id: str) -> list[str]:
        return [
            WORKER,
            "run",
            "--agent",
            agent_id,
            "--trigger",
            SANDBOX_TRIGGER,
            "--cut-dir",
            SANDBOX_CUT_DIR,
        ]

    @staticmethod
    def _parse(stdout: str, agent_id: str, version: str) -> tuple[FindingDraft, ...]:
        try:
            doc = DraftsDocument.loads(stdout)
        except (ValueError, ValidationError) as exc:
            raise ExecutionError(f"worker stdout is not a drafts document: {exc}") from exc
        if (doc.agent_id, doc.agent_version) != (agent_id, version):
            raise ExecutionError(
                f"worker ran {doc.agent_id}@{doc.agent_version}, expected {agent_id}@{version}"
            )
        return doc.drafts

    def _finish(self, name: str, ok: bool, log: structlog.typing.FilteringBoundLogger) -> None:
        if not ok and self._keep_failed:
            log.warning("sandbox.kept_for_debugging", sandbox=name)
            return
        try:
            self._client.delete(name)
        except OpenShellError as exc:
            log.error("sandbox.delete_failed", sandbox=name, error=str(exc))
