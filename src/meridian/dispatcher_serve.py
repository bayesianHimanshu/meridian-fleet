"""meridian-dispatcher serve: the in-cluster side of a Substrate run (phase 4b step 2).

One small HTTP service in its own namespace, with its own Substrate identity (editor on
the meridian atespace only). Per run request from the laptop it creates the actor from
the zone's template (the golden snapshot), resumes it (waiting up to placement_wait for a
free worker), sends the inputs to the actor's /run through atenet-router, reads the actor
back, checks that its only snapshot is the template's golden one, always deletes it, and
answers with the actor's reply and the traceability fields (meridian.sandbox.dispatch).

What it never does:
- suspend, revert or tag: its client's allow-list (DISPATCHER_VERBS) refuses them before
  anything is sent (GuardRefusedError); it has no endpoint that could ask for them;
- keep a run: the inputs and the actor's reply live only in this process's memory for the
  length of the request. Nothing is written to disk (the root filesystem is read-only and
  the only writable path is an in-memory /tmp for kubectl-ate), and the log has one line
  per request with its method, path, status, actor and duration, never a body.

Substrate is reached with the kubectl-ate binary (`--endpoint` ate-api-server, the pod's
projected ServiceAccount token with the API audience). Actors are reached with an mTLS
CONNECT to atenet-router, presenting the pod's podidentity certificate, whose SPIFFE ID
the router's ingress allow-list names.
"""

import argparse
import http.client
import http.server
import json
import os
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from meridian.sandbox import dispatch
from meridian.sandbox import substrate_conformance as sc
from meridian.sandbox.executor import ExecutionError
from meridian.sandbox.substrate import (
    DISPATCHER_VERBS,
    KubectlAteClient,
    NotFoundError,
    SubstrateClient,
    SubstrateError,
)
from meridian.sandbox.substrate_executor import (
    WorkerLostError,
    foreign_snapshots,
    golden_snapshot_of,
    resume_waiting,
    run_in_actor,
)
from meridian.sandbox.substrate_templates import golden_tag_of

MAX_BODY = 64 * 1024 * 1024
KUBE_API = "https://kubernetes.default.svc"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class RouterSend:
    """POST to an actor's port through atenet-router's CONNECT listener (mTLS)."""

    def __init__(
        self,
        *,
        router: str,
        cert_bundle: Path,
        ca_file: Path,
        atespace: str,
        port: int,
        timeout: float,
    ) -> None:
        parts = urlsplit(router)
        if parts.scheme != "https" or not parts.hostname or not parts.port:
            raise ValueError(f"router must be https://host:port, not {router!r}")
        self._host, self._port = parts.hostname, parts.port
        self._cert, self._ca = cert_bundle, ca_file
        self._atespace, self._actor_port, self._timeout = atespace, port, timeout

    def _context(self) -> ssl.SSLContext:
        # Read each time: the kubelet rotates the projected certificate.
        ctx = ssl.create_default_context(cafile=str(self._ca))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(self._cert))
        return ctx

    def __call__(
        self, actor: str, body: bytes, headers: Mapping[str, str], lap: Callable[[str], None]
    ) -> httpx.Response:
        """A run: POST /run with the inputs."""
        return self.request(actor, "POST", "/run", body, headers, lap)

    def request(
        self,
        actor: str,
        method: str,
        path: str,
        body: bytes | None,
        headers: Mapping[str, str] | None = None,
        lap: Callable[[str], None] = lambda _: None,
    ) -> httpx.Response:
        raw = socket.create_connection((self._host, self._port), timeout=self._timeout)
        tls = self._context().wrap_socket(raw, server_hostname=self._host)
        try:
            target = f"actor-upstream:{self._actor_port}"
            tls.sendall(
                (
                    f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                    f"ate-target-actor: {self._atespace}/{actor}\r\n\r\n"
                ).encode()
            )
            status = _read_head(tls)
            if not status.startswith(("HTTP/1.1 200", "HTTP/1.0 200")):
                code = int(status.split()[1]) if len(status.split()) > 1 else 502
                return httpx.Response(code, text=f"router refused CONNECT: {status}")
            lap("connect")
            conn = http.client.HTTPConnection(
                "actor-upstream", self._actor_port, timeout=self._timeout
            )
            conn.sock = tls  # the tunnel: plain HTTP to the actor inside it
            conn.request(method, path, body=body, headers=dict(headers or {}))
            answer = conn.getresponse()
            return httpx.Response(answer.status, content=answer.read())
        except (OSError, http.client.HTTPException) as exc:
            raise httpx.TransportError(f"router path to {actor}: {exc}") from exc
        finally:
            tls.close()


def _read_head(sock: ssl.SSLSocket) -> str:
    """Read a CONNECT answer up to its blank line; return its status line."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(1)
        if not chunk:
            break
        data += chunk
        if len(data) > 16384:
            break
    return data.split(b"\r\n", 1)[0].decode("latin-1")


@dataclass
class Dispatcher:
    """The run logic, apart from HTTP (tests drive it with fakes)."""

    client: SubstrateClient
    send: Callable[[str, bytes, Mapping[str, str], Callable[[str], None]], httpx.Response]
    placement_wait: float
    ready_workers: Callable[[], int]
    sleep: Callable[[float], None] = time.sleep
    # Conformance (phase 4b step 3): a router request of any method and path, this pod's
    # own "ip:port" (an actor must not reach it) and the API server service's address.
    request: sc.Request | None = None
    own_address: str | None = None
    kube_api: tuple[str, int] = sc.KUBE_API

    def __post_init__(self) -> None:
        self._golden: dict[str, str] = {}
        self._lock = threading.Lock()

    def golden(self, template: str) -> str:
        """The template's golden snapshot uuid (templates are immutable: cached)."""
        with self._lock:
            if template in self._golden:
                return self._golden[template]
        tag = golden_tag_of(self.client.get_template(template))
        if tag is None:
            raise ExecutionError(f"template {template} has no golden snapshot")
        uuid = golden_snapshot_of(self.client.get_tag(tag))
        with self._lock:
            self._golden[template] = uuid
        return uuid

    def free_workers(self) -> int:
        return self.ready_workers() - len(self.client.actors())

    def run(
        self, actor: str, template: str, agent_id: str, body: bytes, body_sha: str
    ) -> dispatch.RunAnswer:
        try:
            golden = self.golden(template)
        except (SubstrateError, ExecutionError) as exc:
            return dispatch.RunAnswer(actor=actor, error=f"template {template}: {exc}")
        # A retried request must not meet an actor left by a lost answer: delete it first.
        self._delete_quietly(actor)
        deleted: list[bool] = []

        def delete(name: str) -> None:
            deleted.append(self._delete_quietly(name))

        try:
            reply = run_in_actor(
                client=self.client,
                actor=actor,
                template=template,
                agent_id=agent_id,
                body=body,
                body_sha=body_sha,
                send=self.send,
                resume=lambda name: resume_waiting(
                    self.client, name, self.placement_wait, self.sleep
                ),
                delete=delete,
            )
        except WorkerLostError as exc:
            return dispatch.RunAnswer(
                actor=actor,
                golden_snapshot=golden,
                worker_lost=True,
                worker_pod=exc.worker_pod,
                node=exc.node,
                error=str(exc),
                deleted=all(deleted),
            )
        except ExecutionError as exc:
            return dispatch.RunAnswer(
                actor=actor, golden_snapshot=golden, error=str(exc), deleted=all(deleted)
            )
        foreign = foreign_snapshots(reply.final, golden) if reply.final is not None else []
        return dispatch.RunAnswer(
            actor=actor,
            status_code=reply.status_code,
            actor_response=reply.text,
            final=reply.final,
            phases=list(reply.phases),
            golden_snapshot=golden,
            foreign_snapshots=foreign,
            deleted=all(deleted),
            error=f"snapshots other than the golden one: {foreign}" if foreign else None,
        )

    def conformance(self, template: str) -> dict[str, Any]:
        """Run the containment checks in two data-free actors of a conformance template
        (meridian.sandbox.substrate_conformance.probe_session). Any other template is
        refused: this endpoint never sends a zone actor anything."""
        if not template.startswith(f"{sc.CONFORMANCE_PREFIX}-") or self.request is None:
            raise ValueError(f"{template!r} is not a conformance template")
        return sc.probe_session(
            client=self.client,
            request=self.request,
            resume=lambda name: resume_waiting(self.client, name, self.placement_wait, self.sleep),
            template=template,
            dispatcher=self.own_address,
            kube_api=self.kube_api,
        )

    def _delete_quietly(self, actor: str) -> bool:
        try:
            self.client.delete_actor(actor)
        except NotFoundError:
            return True
        except SubstrateError:
            return False
        return True


def pool_ready(namespace: str, pool: str) -> Callable[[], int]:
    """readyReplicas of the WorkerPool, read with the pod's own ServiceAccount token."""

    def ready() -> int:
        token = (SA_DIR / "token").read_text(encoding="utf-8").strip()
        response = httpx.get(
            f"{KUBE_API}/apis/ate.dev/v1/namespaces/{namespace}/workerpools/{pool}",
            headers={"Authorization": f"Bearer {token}"},
            verify=str(SA_DIR / "ca.crt"),
            timeout=10,
        )
        response.raise_for_status()
        return int(dict(response.json().get("status", {})).get("readyReplicas") or 0)

    return ready


def make_handler(
    dispatcher: Dispatcher, token: Callable[[], str], log: structlog.typing.FilteringBoundLogger
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "meridian-dispatcher"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            pass  # one structured line per request instead, below

        def _answer(self, status: int, payload: object, nonce: str | None) -> None:
            body = json.dumps(payload, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if nonce is not None:
                self.send_header(dispatch.PROOF_HEADER, dispatch.proof(token(), nonce, body))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> str | None:
            """The request's nonce if its bearer token is right, else None (401 sent)."""
            nonce = self.headers.get(dispatch.NONCE_HEADER)
            if not dispatch.bearer_ok(token(), self.headers.get(dispatch.AUTH_HEADER)) or not nonce:
                self._answer(401, {"error": "unauthenticated"}, None)
                return None
            return nonce

        def _handle(self, method: str) -> None:
            start = time.monotonic()
            status, actor = 500, None
            try:
                if method == "GET" and self.path == "/healthz":
                    status = 200
                    self._answer(200, "ok", None)
                    return
                nonce = self._authorized()
                if nonce is None:
                    status = 401
                    return
                status, payload, actor = self._route(method)
                self._answer(status, payload, nonce)
            finally:
                log.info(
                    "dispatcher.request",
                    method=method,
                    path=self.path.split("?")[0][:120],
                    status=status,
                    actor=actor,
                    ms=round((time.monotonic() - start) * 1000),
                )

        def _route(self, method: str) -> tuple[int, Any, str | None]:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            try:
                if method == "GET" and parts[:2] == ["v1", "templates"] and len(parts) == 3:
                    return 200, dispatcher.client.get_template(parts[2]), None
                if method == "GET" and parts[:2] == ["v1", "tags"] and len(parts) == 3:
                    return 200, dispatcher.client.get_tag(parts[2]), None
                if method == "GET" and parts == ["v1", "actors"]:
                    return 200, {"actors": dispatcher.client.actors()}, None
                if method == "GET" and parts == ["v1", "capacity"]:
                    return 200, {"free_workers": dispatcher.free_workers()}, None
                if method == "DELETE" and parts[:2] == ["v1", "actors"] and len(parts) == 3:
                    dispatcher.client.delete_actor(parts[2])
                    return 200, {"deleted": parts[2]}, parts[2]
                if method == "POST" and parts == ["v1", "runs"]:
                    return self._run()
                if method == "POST" and parts == ["v1", "conformance"]:
                    return self._conformance()
            except NotFoundError as exc:
                return 404, {"error": str(exc)[:500]}, None
            except SubstrateError as exc:
                return 502, {"error": str(exc)[:500]}, None
            return 404, {"error": "no such endpoint"}, None

        def _run(self) -> tuple[int, Any, str | None]:
            h = self.headers
            actor, template = h.get(dispatch.ACTOR_HEADER), h.get(dispatch.TEMPLATE_HEADER)
            agent_id, body_sha = h.get("X-Meridian-Agent"), h.get("X-Meridian-Archive-Sha256")
            length = int(h.get("Content-Length") or -1)
            if not (actor and template and agent_id and body_sha) or not 0 < length <= MAX_BODY:
                return 400, {"error": "missing run headers or bad length"}, actor
            body = self.rfile.read(length)
            answer = dispatcher.run(actor, template, agent_id, body, body_sha)
            del body  # nothing of the run outlives the request
            return 200, answer.model_dump(mode="json"), actor

        def _conformance(self) -> tuple[int, Any, str | None]:
            length = int(self.headers.get("Content-Length") or -1)
            if not 0 < length <= 4096:
                return 400, {"error": "bad length"}, None
            try:
                template = str(json.loads(self.rfile.read(length))["template"])
                answer = dispatcher.conformance(template)
            except (ValueError, KeyError, TypeError) as exc:
                return 400, {"error": str(exc)[:300]}, None
            return 200, answer, str(answer["actor"])

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    return Handler


def serve(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="meridian-dispatcher serve")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--atespace", required=True)
    parser.add_argument("--kubectl-ate", default="kubectl-ate")
    parser.add_argument("--api-endpoint", required=True)
    parser.add_argument("--ate-token-file", type=Path, required=True)
    parser.add_argument("--router", required=True)
    parser.add_argument("--router-cert", type=Path, required=True)
    parser.add_argument("--router-ca", type=Path, required=True)
    parser.add_argument("--pool", required=True, help="<namespace>/<WorkerPool>")
    parser.add_argument("--actor-port", type=int, default=8080)
    parser.add_argument("--placement-wait", type=float, default=120.0)
    parser.add_argument("--request-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    log = structlog.get_logger()

    def token() -> str:
        value = args.token_file.read_text(encoding="utf-8").strip()
        if len(value) < dispatch.MIN_TOKEN_BYTES:
            raise RuntimeError("the dispatcher token is missing or too short")
        return str(value)

    token()  # refuse to start without one
    client = KubectlAteClient(
        args.atespace,
        argv0=[args.kubectl_ate],
        endpoint=args.api_endpoint,
        token_file=lambda: args.ate_token_file,
        allowed=DISPATCHER_VERBS,
        timeout_seconds=args.request_timeout,
    )
    namespace, pool = args.pool.split("/", 1)
    router = RouterSend(
        router=args.router,
        cert_bundle=args.router_cert,
        ca_file=args.router_ca,
        atespace=args.atespace,
        port=args.actor_port,
        timeout=args.request_timeout,
    )

    def request(actor: str, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
        response = router.request(actor, method, path, body, {"Content-Type": "application/json"})
        return response.status_code, response.content

    pod_ip = os.environ.get("POD_IP")
    dispatcher = Dispatcher(
        client=client,
        send=router,
        placement_wait=args.placement_wait,
        ready_workers=pool_ready(namespace, pool),
        request=request,
        own_address=f"{pod_ip}:{args.port}" if pod_ip else None,
        kube_api=(
            os.environ.get("KUBERNETES_SERVICE_HOST", sc.KUBE_API[0]),
            int(os.environ.get("KUBERNETES_SERVICE_PORT", str(sc.KUBE_API[1]))),
        ),
    )
    server = http.server.ThreadingHTTPServer(
        ("0.0.0.0", args.port),  # noqa: S104 (a pod listens on its own address)
        make_handler(dispatcher, token, log),
    )
    server.daemon_threads = True
    log.info("dispatcher.listening", port=args.port, atespace=args.atespace)
    server.serve_forever()
    return 0


def entry() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] != "serve":
        print("usage: meridian-dispatcher serve [options]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(serve(argv[1:]))
