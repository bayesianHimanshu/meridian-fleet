"""Where a run happens in Agent Substrate: one actor per run, cloned from a golden snapshot.

For each run the executor stages the same files the OpenShell executor uploads (only the
zone's areas and meta/, meridian.sandbox.executor.stage_run), packs them into one tar,
creates an actor from the zone's ActorTemplate (it starts from the template's golden
snapshot: a warm meridian-worker serve that has seen no data), resumes it, POSTs the tar
to /run through `kubectl ate proxy`, and reads the drafts from the response. Before the
actor is deleted it checks that the actor's only snapshot is that golden one: a run is
never suspended, so no snapshot can hold its data. The actor is always deleted, and
`drain` waits until every actor this executor created is gone.

Substrate has no per-path read rules; the compensating control is that an actor receives
only its zone's folders (docs/threat-model.md), which stage_run builds and
tests/unit/test_substrate_executor.py proves for every zone.
"""

import hashlib
import io
import json
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog

from meridian.agents.registry import AgentRegistry
from meridian.agents.runner import RetryableRunError
from meridian.domain.events import Event, SubstrateRun
from meridian.platform.config import SubstrateSettings
from meridian.sandbox.executor import (
    ExecutionError,
    ExecutionResult,
    OpenShellExecutor,
    RunPlan,
    sandbox_name,
    stage_run,
)
from meridian.sandbox.policies import policy_for_agent, sha256_bytes
from meridian.sandbox.substrate import (
    NotFoundError,
    ResourceExhaustedError,
    SubstrateClient,
    SubstrateError,
)
from meridian.sandbox.substrate_templates import (
    PORT,
    SUBSTRATE_POLICIES,
    committed_image,
    committed_name,
    golden_error_of,
    golden_tag_of,
    probe_command,
    spec_sha256,
    template_path,
    zone_command,
)
from meridian.sandbox.tracking import SandboxTracker
from meridian.worker_serve import AGENT_HEADER, SHA_HEADER

SUBSTRATE = "substrate"
LOG_TAIL = 2000
TAG_OWNER = "SNAPSHOT_OWNER_TAG"


@dataclass(frozen=True, slots=True)
class ZoneTemplate:
    """A deployed ActorTemplate and its golden snapshot, checked against the committed file."""

    policy: str
    name: str
    sha256: str
    file: str
    file_sha256: str
    image: str
    golden_tag: str
    golden_snapshot: str


def archive(stage: Path) -> bytes:
    """An uncompressed tar of <area>/<file> for every staged file, byte for byte the same
    for the same files: sorted, mode 0644, owner 0, mtime 0, no folder entries."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            rel = path.relative_to(stage)
            if len(rel.parts) != 2:
                raise ExecutionError(f"unexpected staged path {rel}")
            info = tarfile.TarInfo(rel.as_posix())
            info.size = path.stat().st_size
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    return buffer.getvalue()


def archive_members(body: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tar:
        return [m.name for m in tar]


def golden_snapshot_of(tag: Mapping[str, Any]) -> str:
    snapshot = dict(dict(tag.get("status", {})).get("snapshot", {}))
    uuid = snapshot.get("uuid")
    if not uuid:
        raise ExecutionError(f"tag {dict(tag.get('metadata', {})).get('name')} has no snapshot")
    return str(uuid)


def load_template(
    client: SubstrateClient, deploy_dir: Path, policy: str, *, probe: bool = False
) -> ZoneTemplate:
    """Read the committed template file, fetch the deployed template it names and check
    that what runs is what is committed (image and command), and that its golden snapshot
    is ready."""
    path = template_path(deploy_dir, policy)
    try:
        name, image = committed_name(path), committed_image(path)
        file_bytes = path.read_bytes()
    except OSError as exc:
        raise ExecutionError(f"no committed template for {policy}: {exc}") from exc
    try:
        server = client.get_template(name)
    except SubstrateError as exc:
        raise ExecutionError(f"template {name} is not deployed: {exc}") from exc
    (container,) = server.get("containers", [{}])
    command = probe_command() if probe else zone_command(policy)
    if container.get("image") != image or list(container.get("command", [])) != command:
        raise ExecutionError(f"deployed template {name} differs from {path}")
    tag = golden_tag_of(server)
    if tag is None:
        error = golden_error_of(server)
        raise ExecutionError(f"template {name} has no golden snapshot yet ({error or 'pending'})")
    return ZoneTemplate(
        policy=policy,
        name=name,
        sha256=spec_sha256(server),
        file=path.as_posix(),
        file_sha256=sha256_bytes(file_bytes),
        image=image,
        golden_tag=tag,
        golden_snapshot=golden_snapshot_of(client.get_tag(tag)),
    )


class WorkerLostError(ExecutionError, RetryableRunError):
    """The actor's worker went away mid-run (a Spot preemption, a node lost): the run
    failed, nothing of it was kept, and the runner may retry it once on a new actor."""


@dataclass(frozen=True, slots=True)
class ActorReply:
    """What one actor run produced, before any check: the actor's HTTP answer, the actor
    as read back before deletion (None if it was gone), and the phase timings."""

    status_code: int
    text: str
    final: dict[str, Any] | None
    phases: tuple[tuple[str, int], ...]


# send(actor, body, headers, lap): lap(step) closes a phase the transport times itself.
Send = Callable[[str, bytes, Mapping[str, str], Callable[[str], None]], httpx.Response]


def crashed(actor: Mapping[str, Any] | None) -> bool:
    """The actor lost its worker: gone, CRASHED, or no worker assignment any more."""
    if actor is None:
        return True
    status = dict(actor.get("status", {}))
    return str(status.get("state", "")).endswith("CRASHED") or not status.get("workerAssignment")


def run_in_actor(
    *,
    client: SubstrateClient,
    actor: str,
    template: str,
    agent_id: str,
    body: bytes,
    body_sha: str,
    send: Send,
    resume: Callable[[str], int],
    delete: Callable[[str], None],
) -> ActorReply:
    """Create the actor from the template (golden snapshot), resume it, send the archive
    to /run, read the actor back, and always delete it. Shared by the laptop's direct
    executor and the in-cluster dispatcher; send is how /run is reached (kubectl ate proxy
    on the laptop, atenet-router in the cluster). A run whose worker is lost raises
    WorkerLostError; any other Substrate or HTTP failure raises ExecutionError."""
    phases: list[tuple[str, int]] = []
    t = time.perf_counter()

    def lap(step: str) -> None:
        nonlocal t
        now = time.perf_counter()
        phases.append((step, round((now - t) * 1000)))
        t = now

    response: httpx.Response | None = None
    final: dict[str, Any] | None = None
    failure: Exception | None = None
    sent = False  # the actor was placed and the request went out
    try:
        client.create_actor(actor, template)
        lap("create")
        waited = resume(actor)
        lap("resume")
        if waited:
            phases.append(("placement_wait", waited))
        sent = True
        response = send(
            actor,
            body,
            {AGENT_HEADER: agent_id, SHA_HEADER: body_sha, "Content-Type": "application/x-tar"},
            lap,
        )
        lap("request")
    except (SubstrateError, httpx.HTTPError) as exc:
        failure = exc
    try:
        final = client.get_actor(actor)
    except SubstrateError:
        final = None
    finally:
        delete(actor)
        lap("delete")
    gateway_error = response is not None and response.status_code in (502, 503, 504)
    if sent and (failure is not None or gateway_error) and crashed(final):
        raise WorkerLostError(f"actor {actor} lost its worker: {failure or response}")
    if response is None:
        raise ExecutionError(f"substrate failed for {actor}: {failure}") from failure
    return ActorReply(response.status_code, response.text, final, tuple(phases))


def resume_waiting(
    client: SubstrateClient,
    actor: str,
    wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Resume, waiting for a free worker if none has room (ResourceExhausted, retried
    with backoff for up to wait_seconds); returns the ms spent waiting."""
    start = time.monotonic()
    delay = 0.25
    while True:
        try:
            client.resume_actor(actor)
            return round((time.monotonic() - start) * 1000) if delay > 0.25 else 0
        except ResourceExhaustedError:
            if time.monotonic() - start + delay > wait_seconds:
                raise
            sleep(delay)
            delay = min(delay * 2, 2.0)


def foreign_snapshots(actor: Mapping[str, Any], golden: str) -> list[str]:
    """Every snapshot of the actor other than the golden one it was created from."""
    snapshots = dict(actor.get("status", {})).get("snapshots", [])
    return [
        str(s.get("uuid", "?"))
        for s in snapshots
        if s.get("uuid") != golden or s.get("owner") != TAG_OWNER
    ]


class SubstrateExecutor:
    name = SUBSTRATE

    def __init__(
        self,
        *,
        client: SubstrateClient,
        registry: AgentRegistry,
        settings: SubstrateSettings,
        templates: Mapping[str, ZoneTemplate],
        image_digest: str,
        study_root: Path,
        image_source_commit: str | None = None,
        tracker: SandboxTracker,
        log: structlog.typing.FilteringBoundLogger,
        safety_root: Path | None = None,
        etmf_root: Path | None = None,
        audit_root: Path | None = None,
        staging_dir: Path | None = None,
        name_factory: Callable[[str, int], str] = sandbox_name,
        post: Callable[..., httpx.Response] = httpx.post,
        delete_confirm_seconds: float = 60.0,
        free_workers: Callable[[], int] | None = None,
        placement_wait_seconds: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """free_workers reports how many workers of the pool hold no actor (capacity());
        a resume that finds no room (ResourceExhausted) is retried for up to
        placement_wait_seconds, so a run waits for a worker instead of failing."""
        unknown = set(templates) - set(SUBSTRATE_POLICIES)
        if unknown:
            raise ExecutionError(f"no substrate template may exist for {sorted(unknown)}")
        self._client = client
        self._registry = registry
        self._settings = settings
        self._templates = dict(templates)
        self._image_digest = image_digest  # the image id, as the OpenShell executor records
        self._image_source_commit = image_source_commit
        self._study_root = study_root
        self._safety_root = safety_root
        self._etmf_root = etmf_root
        self._audit_root = audit_root
        self._staging_dir = staging_dir
        self._tracker = tracker
        self._log = log
        self._name_factory = name_factory
        self._post = post
        self._confirm = delete_confirm_seconds
        self._free_workers = free_workers
        self._placement_wait = placement_wait_seconds
        self._sleep = sleep
        self._created: list[str] = []

    def template_for_policy(self, policy: str) -> ZoneTemplate:
        if policy not in self._templates:
            raise ExecutionError(f"{policy} has no substrate template")
        return self._templates[policy]

    def template_for(self, agent_id: str) -> ZoneTemplate:
        policy = policy_for_agent(agent_id)
        if policy not in self._templates:
            raise ExecutionError(f"{agent_id} runs under {policy}, which has no substrate template")
        return self._templates[policy]

    def placement(self, template: ZoneTemplate, actor: str) -> SubstrateRun:
        s = self._settings
        return SubstrateRun(
            version=s.version,
            commit=s.commit,
            atespace=s.atespace,
            template=template.name,
            template_sha256=template.sha256,
            template_file=template.file,
            template_file_sha256=template.file_sha256,
            image=template.image,
            worker_pool=f"{s.namespace}/{s.worker_pool}",
            sandbox_class=s.sandbox_class,
            actor=actor,
            golden_tag=template.golden_tag,
            golden_snapshot=template.golden_snapshot,
            image_source_commit=self._image_source_commit,
        )

    def executor_name(self, agent_id: str) -> str:
        return self.name

    def capacity(self, executor: str) -> int:
        """The pool's free workers now (one actor per worker), at least 1."""
        if self._free_workers is None:
            return 1
        return max(1, self._free_workers())

    def _resume(self, actor: str) -> int:
        """Resume, waiting for a free worker if none has room; returns ms spent waiting."""
        return resume_waiting(self._client, actor, self._placement_wait, self._sleep)

    def plan(self, agent_id: str, trigger: Event) -> RunPlan:
        template = self.template_for(agent_id)
        return RunPlan(
            self.name, self.placement(template, self._name_factory(agent_id, trigger.seq))
        )

    def run(
        self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None
    ) -> ExecutionResult:
        start = time.perf_counter()
        phases: list[tuple[str, int]] = []
        registered = self._registry.get(agent_id)
        template = self.template_for(agent_id)
        planned = (plan or self.plan(agent_id, trigger)).substrate
        if planned is None or planned.template != template.name:
            raise ExecutionError(f"the plan for {agent_id} is not a substrate placement")
        actor = planned.actor
        log = self._log.bind(agent_id=agent_id, trigger_seq=trigger.seq, actor=actor)
        with tempfile.TemporaryDirectory(prefix="meridian-stage-", dir=self._staging_dir) as tmp:
            staged = stage_run(
                agent_id=agent_id,
                trigger=trigger,
                cut_id=cut_id,
                policy_name=template.policy,
                dest=Path(tmp),
                study_root=self._study_root,
                safety_root=self._safety_root,
                etmf_root=self._etmf_root,
                audit_root=self._audit_root,
            )
            body = archive(staged.stage)
        body_sha = hashlib.sha256(body).hexdigest()
        phases.append(("stage", round((time.perf_counter() - start) * 1000)))
        self._tracker.record(actor, "created", template.name)
        self._created.append(actor)
        reply = self._execute(actor, template, agent_id, body, body_sha, log)
        phases.extend(reply.phases)
        final = reply.final
        result = self._read(reply, registered.agent.agent_id, registered.agent.version, actor)
        if result["archive_sha256"] != body_sha:
            raise ExecutionError(f"actor {actor} received a different archive")
        if result["inputs_manifest_sha256"] != staged.inputs_manifest_sha256:
            raise ExecutionError(f"actor {actor} verified a different inputs manifest")
        if final is None:
            raise ExecutionError(f"actor {actor} disappeared before it was checked")
        foreign = foreign_snapshots(final, template.golden_snapshot)
        if foreign:
            raise ExecutionError(
                f"actor {actor} has snapshots other than its golden one: {foreign}"
            )
        status = dict(final.get("status", {}))
        assignment = dict(status.get("workerAssignment", {}))
        runtime = dict(assignment.get("sandboxRuntime", {}))
        if (
            runtime.get("sandboxClass", self._settings.sandbox_class)
            != self._settings.sandbox_class
        ):
            raise ExecutionError(f"actor {actor} ran in {runtime.get('sandboxClass')}")
        substrate = planned.model_copy(
            update={
                "actor_uid": dict(final.get("metadata", {})).get("uid"),
                "worker_pod": assignment.get("workerPod"),
            }
        )
        out = ExecutionResult(
            drafts=result["drafts"],
            executor=self.name,
            image_digest=self._image_digest,
            policy_sha256=None,
            duration_ms=round((time.perf_counter() - start) * 1000),
            stderr_tail=str(result["log_tail"])[-LOG_TAIL:],
            inputs_manifest_sha256=staged.inputs_manifest_sha256,
            substrate=substrate,
            phases_ms=tuple(phases),
        )
        log.info("substrate.run_done", duration_ms=out.duration_ms, drafts=len(out.drafts))
        return out

    def _execute(
        self,
        actor: str,
        template: ZoneTemplate,
        agent_id: str,
        body: bytes,
        body_sha: str,
        log: structlog.typing.FilteringBoundLogger,
    ) -> ActorReply:
        """The run in its actor, from the laptop: kubectl ate per verb and one proxy."""
        return run_in_actor(
            client=self._client,
            actor=actor,
            template=template.name,
            agent_id=agent_id,
            body=body,
            body_sha=body_sha,
            send=self._send,
            resume=self._resume,
            delete=lambda name: self._delete(name, log),
        )

    def _send(
        self, actor: str, body: bytes, headers: Mapping[str, str], lap: Callable[[str], None]
    ) -> httpx.Response:
        with self._client.proxy(actor, PORT) as address:
            lap("proxy")
            return self._post(
                f"http://{address}/run",
                content=body,
                headers=dict(headers),
                timeout=self._settings.request_timeout_seconds,
            )

    @staticmethod
    def _read(reply: ActorReply, agent_id: str, version: str, actor: str) -> dict[str, Any]:
        if reply.status_code != 200:
            raise ExecutionError(f"actor {actor} answered {reply.status_code}: {reply.text[:500]}")
        try:
            payload = json.loads(reply.text)
        except ValueError as exc:
            raise ExecutionError(f"actor {actor} answered no JSON") from exc
        if payload.get("exit_code") != 0 or payload.get("document") is None:
            tail = str(payload.get("log_tail", ""))[-LOG_TAIL:]
            raise ExecutionError(
                f"meridian-worker exited {payload.get('exit_code')} in {actor}: {tail}"
            )
        drafts = OpenShellExecutor._parse(str(payload["document"]), agent_id, version)
        return {**payload, "drafts": drafts}

    def _delete(self, actor: str, log: structlog.typing.FilteringBoundLogger) -> None:
        self._tracker.record(actor, "delete_requested")
        try:
            self._client.delete_actor(actor)
        except NotFoundError:
            pass
        except SubstrateError as exc:
            self._tracker.record(actor, "delete_failed", str(exc)[:500])
            log.error("substrate.delete_failed", error=str(exc))
            return
        self._tracker.record(actor, "deleted")

    def drain(self) -> None:
        """Wait until no actor this executor created is listed any more."""
        if not self._created:
            return
        deadline = time.monotonic() + self._confirm
        while True:
            try:
                live = {str(dict(a.get("metadata", {})).get("name")) for a in self._client.actors()}
            except SubstrateError as exc:
                self._log.error("substrate.list_failed", error=str(exc))
                return
            left = sorted(set(self._created) & live)
            if not left or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if left:
            self._log.error("substrate.actors_left", actors=left)
        self._created = [a for a in self._created if a in left]


def describe(templates: Mapping[str, ZoneTemplate]) -> str:
    return json.dumps(
        {
            p: {"template": t.name, "golden_snapshot": t.golden_snapshot}
            for p, t in templates.items()
        },
        sort_keys=True,
    )
