"""A thin wrapper around `kubectl ate` (checked against Agent Substrate v1.0.0).

Only what this project is allowed to do: read templates, tags and actors, and create,
resume, proxy and delete actors, all in the atespace this code owns (config [substrate]
atespace). Templates, the atespace and the WorkerPool are set up by `meridian substrate
deploy` (meridian.sandbox.substrate_deploy). Tests inject a fake with the same protocol.

A run reaches its actor through `kubectl ate proxy actor`, which mints a client
certificate, connects to atenet-router and forwards a local port to the actor's port.

Two identities (phase 4a step 3, docs/substrate-design.md):
- The runner acts as its own ServiceAccount, meridian/meridian-runner, with a short-lived
  token (--token-file). Its only grant is `editor` on the atespace (the access policy in
  deploy/substrate/access-policy.yaml), so Substrate refuses it anything outside the
  atespace. Substrate v1.0.0 cannot refuse it suspend: an editor may create, delete,
  suspend, revert and tag actors alike (cmd/ateapi/internal/authz/model.fga).
  So the runner's client also carries a verb allow-list (RUNNER_VERBS) and refuses every
  other `kubectl ate` call (suspend, revert, tag, update) before it is made, raising
  GuardRefusedError. That guard is ours, in this process, not Substrate's.
- Deploying templates and the proofs' positive control use the shared bootstrap identity
  (ate-client), without a guard.
"""

import contextlib
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

KUBECTL = "kubectl"
PROXY_LINE = re.compile(r"Proxying (127\.0\.0\.1:\d+) ->")


class SubstrateError(Exception):
    pass


class NotFoundError(SubstrateError):
    pass


class ResourceExhaustedError(SubstrateError):
    """No worker has room for the actor (gRPC ResourceExhausted)."""


class GuardRefusedError(SubstrateError):
    """The runner's client refused a call outside its allow-list; nothing was sent."""

    def __init__(self, verb: str, kind: str, name: str | None) -> None:
        self.verb, self.kind, self.name = verb, kind, name
        super().__init__(
            f"refused by the runner guard: `kubectl ate {verb} {kind}` is not in the runner's "
            f"allow-list ({', '.join(' '.join(v) for v in sorted(RUNNER_VERBS))})"
        )


# What a run needs, and nothing else. Golden-snapshot resume (resume actor) only restores
# the template's golden snapshot; it never writes one.
RUNNER_VERBS: frozenset[tuple[str, str]] = frozenset(
    {
        ("get", "actor"),
        ("get", "actor-template"),
        ("get", "tag"),
        ("create", "actor"),
        ("resume", "actor"),
        ("proxy", "actor"),
        ("delete", "actor"),
    }
)
# The in-cluster dispatcher's allow-list (phase 4b step 2): the runner's, less the proxy
# (it reaches actors through atenet-router with its own certificate, not `proxy actor`).
DISPATCHER_VERBS: frozenset[tuple[str, str]] = RUNNER_VERBS - {("proxy", "actor")}


class SubstrateClient(Protocol):
    def get_template(self, name: str) -> dict[str, Any]: ...

    def get_tag(self, name: str) -> dict[str, Any]: ...

    def get_actor(self, name: str) -> dict[str, Any] | None: ...

    def actors(self) -> list[dict[str, Any]]: ...

    def create_actor(self, name: str, template: str) -> dict[str, Any]: ...

    def resume_actor(self, name: str) -> dict[str, Any]: ...

    def delete_actor(self, name: str) -> None: ...

    def proxy(self, name: str, port: int) -> contextlib.AbstractContextManager[str]:
        """Forward a local port to the actor's port; yields "127.0.0.1:<local port>"."""
        ...


class KubectlAteClient:
    """Runs `kubectl ate` (the kubectl-ate plugin) against the current kube context."""

    def __init__(
        self,
        atespace: str,
        *,
        kubectl: str = KUBECTL,
        context: str | None = None,
        timeout_seconds: float = 120.0,
        token_file: Callable[[], Path] | None = None,
        allowed: frozenset[tuple[str, str]] | None = None,
        argv0: Sequence[str] | None = None,
        endpoint: str | None = None,
    ) -> None:
        """token_file returns a current token file (the runner's identity); None acts as
        the shared bootstrap identity. allowed, if given, is the verb allow-list. argv0
        replaces `kubectl ate` (the dispatcher runs the kubectl-ate binary itself) and
        endpoint names ate-api-server directly instead of a port-forward."""
        self._atespace = atespace
        self._kubectl = kubectl
        self._context = context
        self._timeout = timeout_seconds
        self._token_file = token_file
        self._allowed = allowed
        self._argv0 = list(argv0) if argv0 is not None else [kubectl, "ate"]
        self._endpoint = endpoint

    @property
    def atespace(self) -> str:
        return self._atespace

    @property
    def guarded(self) -> bool:
        return self._allowed is not None

    def _base(self) -> list[str]:
        base = list(self._argv0)
        if self._endpoint:
            base += ["--endpoint", self._endpoint]
        if self._context:
            base += ["--context", self._context]
        if self._token_file is not None:
            base += ["--token-file", str(self._token_file())]
        return base

    def _guard(self, args: Sequence[str]) -> None:
        verb, kind = [*args, "", ""][:2]
        if self._allowed is not None and (verb, kind) not in self._allowed:
            name = args[2] if len(args) > 2 and not args[2].startswith("-") else None
            raise GuardRefusedError(verb, kind, name)

    def _run(self, args: Sequence[str]) -> str:
        self._guard(args)
        argv = [*self._base(), *args]
        try:
            done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
                argv, capture_output=True, text=True, timeout=self._timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SubstrateError(f"{' '.join(argv[:4])} failed: {exc}") from exc
        if done.returncode != 0:
            message = _error_text(done.stderr)
            if "NotFound" in message or "not found" in message:
                raise NotFoundError(message)
            if "ResourceExhausted" in message:
                raise ResourceExhaustedError(message)
            raise SubstrateError(f"{' '.join(argv[:4])}: {message}")
        return done.stdout

    def call(self, args: Sequence[str]) -> str:
        """Any `kubectl ate` call (guarded like every other); the guard check uses it."""
        return self._run(args)

    def _json(self, args: Sequence[str]) -> dict[str, Any]:
        out = self._run([*args, "-a", self._atespace, "-o", "json"])
        try:
            value = json.loads(out)
        except ValueError as exc:
            raise SubstrateError(f"kubectl ate {args[0]} printed no JSON") from exc
        if not isinstance(value, dict):
            raise SubstrateError(f"kubectl ate {args[0]} printed {type(value).__name__}")
        return value

    def get_template(self, name: str) -> dict[str, Any]:
        return self._json(["get", "actor-template", name])

    def get_tag(self, name: str) -> dict[str, Any]:
        return self._json(["get", "tag", name])

    def get_actor(self, name: str) -> dict[str, Any] | None:
        try:
            return self._json(["get", "actor", name])
        except NotFoundError:
            return None

    def actors(self) -> list[dict[str, Any]]:
        listed = self._json(["get", "actor"])
        return list(listed.get("actors", []))

    def create_actor(self, name: str, template: str) -> dict[str, Any]:
        return self._json(["create", "actor", name, "--template", template])

    def resume_actor(self, name: str) -> dict[str, Any]:
        return self._json(["resume", "actor", name])

    def delete_actor(self, name: str) -> None:
        self._run(["delete", "actor", name, "-a", self._atespace])

    def suspend_actor(self, name: str) -> dict[str, Any]:
        """Only the canary proof's positive control suspends an actor, with the bootstrap
        identity; the runner's guarded client refuses it."""
        return self._json(["suspend", "actor", name, "--mode", "sync"])

    def tag_actor(self, name: str, tag: str) -> dict[str, Any]:
        """Never used by a run; here so the guard's refusal of it can be tested."""
        return self._json(["create", "tag", tag, "--actor", name])

    def revert_actor(self, name: str) -> dict[str, Any]:
        """Never used by a run; here so the guard's refusal of it can be tested."""
        return self._json(["revert", "actor", name])

    @contextlib.contextmanager
    def proxy(self, name: str, port: int) -> Iterator[str]:
        self._guard(["proxy", "actor", name])
        argv = [*self._base(), "proxy", "actor", name, "-a", self._atespace, f"0:{port}"]
        process = subprocess.Popen(  # noqa: S603 (argv built from fixed parts)
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        try:
            address = _await_proxy(process, self._timeout)
            yield address
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _await_proxy(process: subprocess.Popen[str], timeout: float) -> str:
    assert process.stderr is not None
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        line = process.stderr.readline()
        if not line:
            if process.poll() is not None:
                raise SubstrateError(f"kubectl ate proxy exited: {_error_text(''.join(seen))}")
            continue
        seen.append(line)
        match = PROXY_LINE.search(line)
        if match:
            return match.group(1)
    raise SubstrateError("kubectl ate proxy did not start listening")


class ServiceAccountToken:
    """A short-lived token for a ServiceAccount (kubectl create token), kept in a 0600 file
    and minted again when it is older than half its lifetime. Thread safe."""

    def __init__(
        self,
        *,
        path: Path,
        namespace: str,
        service_account: str,
        audience: str,
        kubectl: str = KUBECTL,
        context: str | None = None,
        lifetime_seconds: int = 3600,
    ) -> None:
        self._path = path
        self._argv = [kubectl]
        if context:
            self._argv += ["--context", context]
        self._argv += [
            "-n",
            namespace,
            "create",
            "token",
            service_account,
            "--audience",
            audience,
            "--duration",
            f"{lifetime_seconds}s",
        ]
        self._lifetime = lifetime_seconds
        self._minted_at: float | None = None
        self._lock = threading.Lock()

    def __call__(self) -> Path:
        with self._lock:
            now = time.monotonic()
            if self._minted_at is None or now - self._minted_at > self._lifetime / 2:
                done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
                    self._argv, capture_output=True, text=True, timeout=60, check=False
                )
                if done.returncode != 0 or not done.stdout.strip():
                    raise SubstrateError(f"cannot mint the runner token: {done.stderr[-500:]}")
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.touch(mode=0o600)
                tmp.chmod(0o600)
                tmp.write_text(done.stdout.strip(), encoding="utf-8")
                tmp.replace(self._path)
                self._minted_at = now
            return self._path


def pool_ready_workers(kubectl: str, context: str | None, namespace: str, pool: str) -> int:
    """readyReplicas of the WorkerPool (Kubernetes status; the runner's Substrate identity
    may not list workers, which are global)."""
    argv = [kubectl, *(["--context", context] if context else []), "-n", namespace]
    argv += ["get", "workerpool", pool, "-o", "jsonpath={.status.readyReplicas}"]
    done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
        argv, capture_output=True, text=True, timeout=60, check=False
    )
    if done.returncode != 0:
        raise SubstrateError(f"cannot read WorkerPool {namespace}/{pool}: {done.stderr[-300:]}")
    return int(done.stdout.strip() or 0)


def free_workers(client: KubectlAteClient, ready: Callable[[], int]) -> int:
    """Ready workers minus live actors in the atespace (the pool serves only it, and
    holds one actor per worker)."""
    return ready() - len(client.actors())


def _error_text(stderr: str) -> str:
    """stderr without the shared-identity warning kubectl ate prints on every call."""
    lines = [
        line
        for line in stderr.splitlines()
        if line.strip() and not line.startswith("Warning: no --token-file given")
    ]
    return " ".join(lines)[-1000:] or "no error text"


def read_install_record(path: Path) -> dict[str, str]:
    """substrateVersion and context from ate-setup's install record (a small YAML file it
    writes under ~/.cache/ate-setup/installs/<context>.yaml). Only flat `key: value`
    lines under `cluster:` are read; nothing else is trusted from it."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SubstrateError(f"cannot read the ate-setup install record {path}: {exc}") from exc
    for line in text.splitlines():
        stripped = line.strip()
        for key in ("substrateVersion", "outcome", "context"):
            if stripped.startswith(f"{key}:"):
                out.setdefault(key, stripped.split(":", 1)[1].strip().strip('"'))
    return out
