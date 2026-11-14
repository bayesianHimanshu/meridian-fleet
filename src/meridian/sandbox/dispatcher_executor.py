"""The laptop side of a dispatched Substrate run (phase 4b step 2).

DispatcherExecutor is the SubstrateExecutor with one difference: the actor's life (create,
resume, /run, read back, delete) happens in the cluster, in the dispatcher
(meridian.dispatcher_serve), in one request. Everything the laptop did before stays on
the laptop: staging only the zone's folders, the archive and its sha256, checking the
committed template against the deployed one, checking the actor's answer (archive sha256,
inputs manifest sha256, only the golden snapshot, the sandbox class) and the ledger, which
the runner still appends in its fixed order.

The laptop holds no Substrate token. It reaches the dispatcher through one long-lived
`kubectl port-forward` (PortForward) and authenticates both ways with the shared token
(meridian.sandbox.dispatch). The token is read from a 0600 file outside the repository
and never logged.
"""

import contextlib
import json
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Self

import httpx
import structlog

from meridian.domain.events import SubstrateRun
from meridian.sandbox import dispatch
from meridian.sandbox.executor import ExecutionError
from meridian.sandbox.substrate import GuardRefusedError, NotFoundError, SubstrateError
from meridian.sandbox.substrate_executor import (
    ActorReply,
    SubstrateExecutor,
    WorkerLostError,
    ZoneTemplate,
    foreign_snapshots,
)

FORWARD_LINE = re.compile(r"Forwarding from (127\.0\.0\.1:\d+) ->")


class DispatcherError(SubstrateError):
    pass


def read_token(path: Path) -> str:
    """The laptop's half of the shared token: a file only its owner may read."""
    path = path.expanduser()
    try:
        mode = path.stat().st_mode
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DispatcherError(f"no dispatcher token at {path}: {exc}") from exc
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise DispatcherError(f"{path} must be readable by its owner only (chmod 600)")
    if len(value) < dispatch.MIN_TOKEN_BYTES:
        raise DispatcherError(f"the token in {path} is too short")
    return value


def write_token(path: Path, token: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    path.chmod(0o600)


class PortForward:
    """One `kubectl port-forward` to the dispatcher, kept open for a whole pass and
    restarted if it dies. address() is thread safe."""

    def __init__(self, argv: list[str], timeout: float = 30.0) -> None:
        self._argv = argv
        self._timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._address: str | None = None
        self._lock = threading.Lock()
        self.starts = 0

    def address(self) -> str:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._start()
            assert self._address is not None
            return self._address

    def restart(self) -> None:
        with self._lock:
            self._stop()
            self._start()

    def _start(self) -> None:
        self._process = subprocess.Popen(  # noqa: S603 (argv built from fixed parts)
            self._argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert self._process.stdout is not None
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            line = self._process.stdout.readline()
            if not line and self._process.poll() is not None:
                err = self._process.stderr.read() if self._process.stderr else ""
                raise DispatcherError(f"kubectl port-forward exited: {err[-500:]}")
            match = FORWARD_LINE.search(line)
            if match:
                self._address = match.group(1)
                self.starts += 1
                # Keep reading its output so the pipe never fills.
                threading.Thread(target=self._drain, args=(self._process,), daemon=True).start()
                return
        self._stop()
        raise DispatcherError("kubectl port-forward did not start listening")

    @staticmethod
    def _drain(process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for _ in process.stdout:
            pass

    def _stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._process, self._address = None, None

    def close(self) -> None:
        with self._lock:
            self._stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class DispatcherClient:
    """The dispatcher's API, as the SubstrateClient protocol for the reads the laptop
    still makes (templates, tags, actors) plus run(). The laptop cannot create, resume,
    proxy, suspend, revert or tag: those raise GuardRefusedError without sending."""

    def __init__(
        self,
        *,
        address: Callable[[], str],
        token: str,
        timeout: float,
        on_transport_error: Callable[[], None] | None = None,
        http: httpx.Client | None = None,
        atespace: str = "meridian",
    ) -> None:
        self._address = address
        self._token = token
        self._timeout = timeout
        self._reconnect = on_transport_error
        self._http = http or httpx.Client()
        self.atespace = atespace

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in (1, 2):
            nonce = dispatch.new_nonce()
            sent = {
                dispatch.AUTH_HEADER: f"Bearer {self._token}",
                dispatch.NONCE_HEADER: nonce,
                **(headers or {}),
            }
            try:
                response = self._http.request(
                    method,
                    f"http://{self._address()}{path}",
                    content=body,
                    headers=sent,
                    timeout=self._timeout,
                )
            except httpx.TransportError as exc:
                if attempt == 2 or self._reconnect is None:
                    raise DispatcherError(f"dispatcher unreachable: {exc}") from exc
                self._reconnect()  # the tunnel dropped: one new tunnel, one retry
                continue
            if response.status_code == 401:
                raise DispatcherError("the dispatcher refused our token (401)")
            if not dispatch.proof_ok(
                self._token, nonce, response.content, response.headers.get(dispatch.PROOF_HEADER)
            ):
                raise DispatcherError("the dispatcher's answer has no valid proof; refused")
            return response
        raise AssertionError("unreachable")

    def _json(self, method: str, path: str) -> dict[str, Any]:
        response = self._call(method, path)
        if response.status_code == 404:
            raise NotFoundError(response.text[:500])
        if response.status_code != 200:
            raise DispatcherError(f"{method} {path}: {response.status_code} {response.text[:500]}")
        value = response.json()
        if not isinstance(value, dict):
            raise DispatcherError(f"{method} {path} answered {type(value).__name__}")
        return value

    def get_template(self, name: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/templates/{name}")

    def get_tag(self, name: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/tags/{name}")

    def actors(self) -> list[dict[str, Any]]:
        return list(self._json("GET", "/v1/actors").get("actors", []))

    def get_actor(self, name: str) -> dict[str, Any] | None:
        for actor in self.actors():
            if dict(actor.get("metadata", {})).get("name") == name:
                return dict(actor)
        return None

    def free_workers(self) -> int:
        return int(self._json("GET", "/v1/capacity").get("free_workers", 0))

    def delete_actor(self, name: str) -> None:
        self._json("DELETE", f"/v1/actors/{name}")

    def run(
        self, *, actor: str, template: str, agent_id: str, body: bytes, body_sha: str
    ) -> dispatch.RunAnswer:
        response = self._call(
            "POST",
            "/v1/runs",
            body=body,
            headers={
                dispatch.ACTOR_HEADER: actor,
                dispatch.TEMPLATE_HEADER: template,
                "X-Meridian-Agent": agent_id,
                "X-Meridian-Archive-Sha256": body_sha,
                "Content-Type": "application/x-tar",
            },
        )
        if response.status_code != 200:
            raise DispatcherError(f"run {actor}: {response.status_code} {response.text[:500]}")
        return dispatch.RunAnswer.model_validate_json(response.content)

    def conformance(self, template: str) -> dict[str, Any]:
        """The containment checks in two data-free actors of the conformance template, run
        by the dispatcher (substrate_conformance.probe_session)."""
        response = self._call(
            "POST",
            "/v1/conformance",
            body=json.dumps({"template": template}).encode(),
            headers={"Content-Type": "application/json"},
        )
        if response.status_code != 200:
            raise DispatcherError(f"conformance: {response.status_code} {response.text[:500]}")
        value = response.json()
        if not isinstance(value, dict):
            raise DispatcherError("conformance: the dispatcher answered no object")
        return value

    def lookup(self, name: str) -> int:
        """GET /v1/templates/<name> and return the status only: a marker in a request
        path is the positive control of the leak check's log searches (the dispatcher logs
        each request's path)."""
        return self._call("GET", f"/v1/templates/{name}").status_code

    def _refuse(self, verb: str, kind: str, name: str | None) -> GuardRefusedError:
        return GuardRefusedError(verb, kind, name)

    def create_actor(self, name: str, template: str) -> dict[str, Any]:
        raise self._refuse("create", "actor", name)

    def resume_actor(self, name: str) -> dict[str, Any]:
        raise self._refuse("resume", "actor", name)

    def proxy(self, name: str, port: int) -> contextlib.AbstractContextManager[str]:
        raise self._refuse("proxy", "actor", name)

    def suspend_actor(self, name: str) -> dict[str, Any]:
        raise self._refuse("suspend", "actor", name)

    def revert_actor(self, name: str) -> dict[str, Any]:
        raise self._refuse("revert", "actor", name)

    def tag_actor(self, name: str, tag: str) -> dict[str, Any]:
        raise self._refuse("create", "tag", tag)


class DispatcherExecutor(SubstrateExecutor):
    """A SubstrateExecutor whose actor runs happen in the dispatcher."""

    def __init__(
        self,
        *,
        dispatcher: DispatcherClient,
        dispatcher_image: str,
        **kwargs: Any,  # noqa: ANN401 (SubstrateExecutor's keywords)
    ) -> None:
        super().__init__(client=dispatcher, free_workers=dispatcher.free_workers, **kwargs)
        self._dispatcher = dispatcher
        self._dispatcher_image = dispatcher_image

    def placement(self, template: ZoneTemplate, actor: str) -> SubstrateRun:
        return (
            super()
            .placement(template, actor)
            .model_copy(update={"dispatcher_image": self._dispatcher_image})
        )

    def _execute(
        self,
        actor: str,
        template: ZoneTemplate,
        agent_id: str,
        body: bytes,
        body_sha: str,
        log: structlog.typing.FilteringBoundLogger,
    ) -> ActorReply:
        start = time.perf_counter()
        try:
            answer = self._dispatcher.run(
                actor=actor, template=template.name, agent_id=agent_id, body=body, body_sha=body_sha
            )
        except SubstrateError as exc:
            self._tracker.record(actor, "delete_failed", f"answer lost: {exc}"[:500])
            raise ExecutionError(f"dispatcher failed for {actor}: {exc}") from exc
        total = round((time.perf_counter() - start) * 1000)
        self._tracker.record(actor, "deleted" if answer.deleted else "delete_failed", answer.error)
        if answer.worker_lost:
            raise WorkerLostError(
                answer.error or f"actor {actor} lost its worker",
                worker_pod=answer.worker_pod,
                node=answer.node,
            )
        if (
            answer.golden_snapshot is not None
            and answer.golden_snapshot != template.golden_snapshot
        ):
            raise ExecutionError(
                f"the dispatcher checked golden {answer.golden_snapshot}, "
                f"not {template.golden_snapshot}"
            )
        if answer.status_code is None or answer.actor_response is None:
            raise ExecutionError(f"dispatcher: {answer.error or 'no answer from the actor'}")
        if answer.final is not None and foreign_snapshots(answer.final, template.golden_snapshot):
            raise ExecutionError(f"actor {actor} has snapshots other than its golden one")
        if answer.error:
            raise ExecutionError(f"dispatcher: {answer.error}")
        inside = sum(ms for _, ms in answer.phases)
        phases = (*answer.phases, ("tunnel", max(0, total - inside)))
        log.debug("dispatcher.run", actor=actor, ms=total)
        return ActorReply(answer.status_code, answer.actor_response, answer.final, tuple(phases))
