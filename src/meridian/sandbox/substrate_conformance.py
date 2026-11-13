"""meridian substrate conformance: what a zone's actor cannot do, shown in real actors.

Substrate has no exec, so two actors are created from the conformance template
(deploy/substrate/templates/conformance.yaml: the zone templates' image, WorkerPool,
sandbox class, size and absent egress policy, with `meridian-worker probe` as command),
and the first is asked over HTTP to try:

* egress_test_net      TCP to 192.0.2.1:443 (RFC 5737)
* egress_host_ollama   TCP to 172.17.0.1:11434 (the host, where Ollama listens)
* egress_peer_actor    TCP to the second actor's worker pod IP, port 8080, where it listens
* egress_kube_api      TCP to the cluster's API server service (10.96.0.1:443 on kind;
                       the dispatcher reports its KUBERNETES_SERVICE_HOST)
* egress_metadata      TCP to 169.254.169.254:80 (GKE's metadata server: Workload Identity
                       tokens; phase 4b step 3)
* egress_by_name       resolving api.anthropic.com (no egress policy: NXDOMAIN)
* write_opt_meridian   writing /opt/meridian/conformance (root's, read-only to uid 1000)
* control_tmp          writing /tmp/conformance (must succeed)
* control_self         TCP to 127.0.0.1:8080, the probe itself (must get its HTTP reply)
* control_peer         the second actor answers GET /readyz through the router (must)
* read_etc             reading /etc/os-release: allowed. Substrate has no per-path read
                       rules; this records the accepted loss (docs/threat-model.md).

In an actor, connect() to any address succeeds locally: atunnel accepts every TCP
connection and tunnels it to atenet-egress, which applies the policy (no policy: deny).
A network check therefore counts as refused when connect() failed, when the reply is
the gateway's own refusal (HTTP 400 with no Server header from an origin) or when the
connection closed with no data; any other reply is a real origin answering, which fails
the check. The gateway's log lines for the refused destinations are collected as
evidence (source substrate_log) when it writes them.

Since phase 4b step 3 the dispatcher runs the session (POST /v1/conformance: it creates
both actors from the conformance template, the only template it accepts there, sends the
checks through atenet-router, which admits only the dispatcher, and deletes both); the
laptop classifies the answers and reads atenet-egress's log.

Each zone's run records PolicyDenied events and a ContainmentCheckCompleted (executor
substrate) as `meridian sandbox conformance` does for OpenShell. The zone-specific part,
which folders an actor receives, is tested by the staging test for every zone.
"""

import contextlib
import json
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from meridian.domain.enums import Visibility
from meridian.domain.events import (
    ContainmentCheck,
    ContainmentCheckCompleted,
    EventDraft,
    PolicyDenied,
    SubstrateRun,
)
from meridian.platform.clock import Clock
from meridian.sandbox.policies import ALL_POLICIES
from meridian.sandbox.substrate import SubstrateClient, SubstrateError
from meridian.sandbox.substrate_executor import ZoneTemplate
from meridian.sandbox.substrate_templates import PORT

ACTOR = "system.conformance"
CONFORMANCE_PREFIX = "conformance"  # substrate_templates.CONFORMANCE: template names start so
TEST_NET = "192.0.2.1"
OLLAMA = ("172.17.0.1", 11434)
KUBE_API = ("10.96.0.1", 443)
METADATA = ("169.254.169.254", 80)
NETWORK: Literal["network"] = "network"
Outcome = Literal["refused", "allowed", "inconclusive"]


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    name: str
    expect: Literal["refused", "allowed"]
    kind: Literal["network", "write", "read", "resolve", "self", "peer"]
    request: Mapping[str, object] = field(default_factory=dict)
    target: str = ""


def specs(
    peer_ip: str, dispatcher: str | None = None, kube_api: tuple[str, int] = KUBE_API
) -> tuple[ProbeSpec, ...]:
    """The probes; dispatcher ("ip:port"), when the dispatcher is deployed, adds one more:
    an actor must not reach the dispatcher (phase 4b step 2)."""

    def tcp(name: str, host: str, port: int) -> ProbeSpec:
        return ProbeSpec(
            name,
            "refused",
            NETWORK,
            {"check": "tcp_connect", "host": host, "port": port},
            f"{host}:{port}",
        )

    extra: tuple[ProbeSpec, ...] = ()
    if dispatcher is not None:
        host, port = dispatcher.rsplit(":", 1)
        extra = (tcp("egress_dispatcher", host, int(port)),)
    return (
        *extra,
        tcp("egress_test_net", TEST_NET, 443),
        tcp("egress_host_ollama", *OLLAMA),
        tcp("egress_peer_actor", peer_ip, PORT),
        tcp("egress_kube_api", *kube_api),
        tcp("egress_metadata", *METADATA),
        ProbeSpec(
            "egress_by_name",
            "refused",
            "resolve",
            {"check": "resolve", "host": "api.anthropic.com"},
            "api.anthropic.com",
        ),
        ProbeSpec(
            "write_opt_meridian",
            "refused",
            "write",
            {"check": "write", "path": "/opt/meridian/conformance"},
            "/opt/meridian/conformance",
        ),
        ProbeSpec(
            "control_tmp",
            "allowed",
            "write",
            {"check": "write", "path": "/tmp/conformance"},  # noqa: S108 (inside the actor)
            "/tmp/conformance",  # noqa: S108
        ),
        ProbeSpec(
            "control_self",
            "allowed",
            "self",
            {"check": "tcp_connect", "host": "127.0.0.1", "port": PORT},
            f"127.0.0.1:{PORT}",
        ),
        ProbeSpec("control_peer", "allowed", "peer", {}, "the second actor's /readyz"),
        ProbeSpec(
            "read_etc",
            "allowed",
            "read",
            {"check": "read", "path": "/etc/os-release"},
            "/etc/os-release",
        ),
    )


def gateway_refusal(answer: Mapping[str, Any]) -> bool:
    """The egress gateway's refusal of a connection with no matching rule."""
    reply = str(answer.get("reply") or "")
    return reply.startswith("HTTP/1.1 400") and not answer.get("server")


def classify(
    spec: ProbeSpec, answer: Mapping[str, Any] | None, error: str | None
) -> ContainmentCheck:
    if answer is None:
        outcome: Outcome = "inconclusive"
        message = error or "no answer"
    else:
        message = str(answer.get("message", ""))[-500:]
        ok = answer.get("outcome") == "ok"
        if spec.kind == NETWORK:
            if not answer.get("connected"):
                outcome = (
                    "refused"
                    if answer.get("outcome") in ("refused", "timeout", "unreachable")
                    else "inconclusive"
                )
            elif gateway_refusal(answer):
                outcome = "refused"
                message = f"atenet-egress answered {answer.get('reply')!r}; {message}"
            elif answer.get("closed") and not answer.get("reply"):
                outcome = "refused"
                message = f"closed with no data; {message}"
            else:
                outcome = "allowed"
        elif spec.kind == "self":
            outcome = (
                "allowed"
                if ok and str(answer.get("reply", "")).startswith("HTTP/1.")
                else "inconclusive"
            )
        elif spec.kind == "resolve":
            outcome = (
                "allowed"
                if ok
                else ("refused" if answer.get("outcome") == "unreachable" else "inconclusive")
            )
        elif spec.kind == "peer":
            outcome = "allowed" if ok else "inconclusive"
        else:  # write, read
            outcome = (
                "allowed"
                if ok
                else ("refused" if answer.get("outcome") == "refused" else "inconclusive")
            )
    return ContainmentCheck(
        name=spec.name,
        expect=spec.expect,
        command=json.dumps(dict(spec.request), sort_keys=True) if spec.request else spec.target,
        outcome=outcome,
        exit_code=None if answer is None else answer.get("errno"),
        message=message,
        passed=outcome == spec.expect,
    )


@dataclass(frozen=True, slots=True)
class SubstrateConformance:
    zone: str  # the policy whose actors are covered (they share the conformance template)
    actor: str
    peer: str
    peer_ip: str
    placement: SubstrateRun
    image_digest: str
    zone_template: ZoneTemplate
    checks: tuple[ContainmentCheck, ...]
    gateway_lines: tuple[str, ...]
    dispatcher: str | None = None  # the dispatcher's "ip:port" when it was probed too
    kube_api: tuple[str, int] = KUBE_API

    def specs(self) -> tuple[ProbeSpec, ...]:
        return specs(self.peer_ip, self.dispatcher, self.kube_api)

    @property
    def all_refused(self) -> bool:
        return all(c.outcome == "refused" for c in self.checks if c.expect == "refused")

    @property
    def log_lines_complete(self) -> bool:
        """atenet-egress logged a refusal for every network target."""
        wanted = {s.target for s in self.specs() if s.kind == NETWORK}
        return all(any(f"authority={t} " in ln for ln in self.gateway_lines) for t in wanted)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks) and self.log_lines_complete

    def failures(self) -> list[str]:
        out = [
            f"{c.name}: expected {c.expect}, got {c.outcome} ({c.message})"
            for c in self.checks
            if not c.passed
        ]
        if not self.log_lines_complete:
            out.append("atenet-egress did not log a refusal for every network target")
        return out


def gateway_lines(
    kubectl: str,
    context: str,
    actor: str,
    targets: Sequence[str],
    since_seconds: int,
    timeout_seconds: float = 30.0,
) -> tuple[str, ...]:
    """atenet-egress access log lines for the actor's connections to the targets:
    `[egress] authority=<host:port> peer_san=spiffe://.../<atespace>/<actor> ... code=403`.
    Polled until every target has a line or the timeout passes (the log lags)."""
    deadline = time.monotonic() + timeout_seconds
    found: dict[str, str] = {}
    while True:
        try:
            done = subprocess.run(  # noqa: S603 (fixed argv)
                [
                    kubectl,
                    "--context",
                    context,
                    "-n",
                    "ate-system",
                    "logs",
                    "deployment/atenet-egress",
                    "--all-containers",
                    f"--since={since_seconds}s",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except OSError, subprocess.TimeoutExpired:
            return tuple(found.values())
        for line in done.stdout.splitlines():
            for target in targets:
                if f"authority={target} " in line and line.split("peer_san=", 1)[-1].split(" ")[
                    0
                ].endswith(f"/{actor}"):
                    found.setdefault(target, line[:2000])
        if len(found) == len(targets) or time.monotonic() >= deadline:
            return tuple(found[t] for t in targets if t in found)
        time.sleep(2.0)


Request = Callable[[str, str, str, bytes | None], tuple[int, bytes]]


def probe_session(
    *,
    client: SubstrateClient,
    request: Request,
    resume: Callable[[str], object],
    template: str,
    dispatcher: str | None,
    kube_api: tuple[str, int],
    names: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """The in-cluster half (the dispatcher's POST /v1/conformance): create the probe actor
    and its peer from the conformance template, send every check through the router
    (request(actor, method, path, body) -> (status, body)), read the actor back, delete
    both. Only data-free conformance actors are ever made here."""
    actor, peer = names or (f"mer-cf-{uuid.uuid4().hex[:8]}", f"mer-cf-{uuid.uuid4().hex[:8]}")
    created: list[str] = []
    answers: list[dict[str, Any]] = []
    peer_ip = "0.0.0.0"  # noqa: S104 (an address to try, not bind)
    final: dict[str, Any] = {}
    try:
        for name in (actor, peer):
            client.create_actor(name, template)
            created.append(name)
            resume(name)
        peer_state = client.get_actor(peer) or {}
        ips = dict(dict(peer_state.get("status", {})).get("workerAssignment", {})).get(
            "workerPodIps", []
        )
        peer_ip = str(ips[0]) if ips else peer_ip
        for spec in specs(peer_ip, dispatcher, kube_api):
            entry: dict[str, Any] = {"name": spec.name, "answer": None, "error": None}
            try:
                if spec.kind == "peer":
                    status, _ = request(peer, "GET", "/readyz", None)
                    entry["answer"] = {
                        "outcome": "ok" if status == 200 else "error",
                        "message": f"GET /readyz answered {status}",
                    }
                else:
                    body = json.dumps(dict(spec.request)).encode()
                    status, raw = request(actor, "POST", "/check", body)
                    if status != 200:
                        raise ValueError(f"/check answered {status}")
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError("/check answered no object")
                    entry["answer"] = value
            except (OSError, ValueError, httpx.HTTPError, SubstrateError) as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
            answers.append(entry)
        final = client.get_actor(actor) or {}
    finally:
        for name in created:
            with contextlib.suppress(SubstrateError):
                client.delete_actor(name)
    return {
        "actor": actor,
        "peer": peer,
        "peer_ip": peer_ip,
        "dispatcher": dispatcher,
        "kube_api": list(kube_api),
        "answers": answers,
        "final": final,
    }


def run_substrate_conformance(
    *,
    session: Callable[[str], Mapping[str, Any]],
    zone: str,
    zone_template: ZoneTemplate,
    probe_template: ZoneTemplate,
    placement: Callable[[ZoneTemplate, str], SubstrateRun],
    image_digest: str,
    kubectl: str,
    context: str,
) -> SubstrateConformance:
    """The laptop's half: session(template) runs probe_session in the cluster (the
    dispatcher); its answers are classified here and atenet-egress's log is read."""
    started = time.monotonic()
    answer = session(probe_template.name)
    actor, peer, peer_ip = str(answer["actor"]), str(answer["peer"]), str(answer["peer_ip"])
    dispatcher = answer.get("dispatcher")
    host, port = list(answer.get("kube_api") or KUBE_API)
    kube_api = (str(host), int(port))
    by_name = {str(a["name"]): a for a in answer.get("answers", [])}
    checks: list[ContainmentCheck] = []
    for spec in specs(peer_ip, dispatcher, kube_api):
        got = by_name.get(spec.name)
        if got is None:
            checks.append(classify(spec, None, "the dispatcher sent no answer"))
        else:
            checks.append(classify(spec, got.get("answer"), got.get("error")))
    since = int(time.monotonic() - started) + 30
    targets = [s.target for s in specs(peer_ip, dispatcher, kube_api) if s.kind == NETWORK]
    lines = gateway_lines(kubectl, context, actor, targets, since)
    final = dict(answer.get("final") or {})
    base = placement(probe_template, actor)
    assignment = dict(dict(final.get("status", {})).get("workerAssignment", {}))
    return SubstrateConformance(
        zone=zone,
        actor=actor,
        peer=peer,
        peer_ip=peer_ip,
        placement=base.model_copy(
            update={
                "actor_uid": dict(final.get("metadata", {})).get("uid"),
                "worker_pod": assignment.get("workerPod"),
            }
        ),
        image_digest=image_digest,
        zone_template=zone_template,
        checks=tuple(checks),
        gateway_lines=lines,
        dispatcher=None if dispatcher is None else str(dispatcher),
        kube_api=kube_api,
    )


def ledger_drafts(result: SubstrateConformance, clock: Clock) -> list[EventDraft]:
    """PolicyDenied for every refusal (from the gateway log when it names the target, else
    from the probe), then ContainmentCheckCompleted with executor substrate."""
    now = clock.now()
    zone = ALL_POLICIES[result.zone].zone
    template = result.zone_template
    drafts: list[EventDraft] = []

    def draft(payload: PolicyDenied | ContainmentCheckCompleted) -> EventDraft:
        return EventDraft(
            occurred_at=now, actor=ACTOR, visibility=Visibility.BLINDED, payload=payload
        )

    by_name = {s.name: s for s in result.specs()}
    for check in result.checks:
        if check.expect != "refused" or check.outcome != "refused":
            continue
        target = by_name[check.name].target
        line = next((ln for ln in result.gateway_lines if f"authority={target} " in ln), None)
        drafts.append(
            draft(
                PolicyDenied(
                    agent_id=ACTOR,
                    resource=target,
                    policy=template.file,
                    reason=check.message or "refused",
                    source="substrate_log" if line else "harness",
                    zone=zone,
                    check=check.name,
                    sandbox=result.actor,
                    policy_sha256=template.sha256,
                    raw_line=line,
                )
            )
        )
    drafts.append(
        draft(
            ContainmentCheckCompleted(
                zone=zone,
                sandbox=result.actor,
                image=template.image,
                image_digest=result.image_digest,
                policy_sha256=template.sha256,
                checks=result.checks,
                log_line_found=result.log_lines_complete,
                all_refused=result.all_refused,
                passed=result.passed,
                policy_name=result.zone if result.zone != zone else None,
                executor="substrate",
                substrate=result.placement,
            )
        )
    )
    return drafts
