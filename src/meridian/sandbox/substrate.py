"""A thin wrapper around `kubectl ate` (checked against Agent Substrate v1.0.0).

Only what this project is allowed to do: read templates, tags and actors, and create,
resume, proxy and delete actors, all in the atespace this code owns (config [substrate]
atespace). Templates, the atespace and the WorkerPool are set up by `meridian substrate
deploy` (meridian.sandbox.substrate_deploy). Tests inject a fake with the same protocol.

A run reaches its actor through `kubectl ate proxy actor`, which mints a client
certificate, connects to atenet-router and forwards a local port to the actor's port.
"""

import contextlib
import json
import re
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

KUBECTL = "kubectl"
PROXY_LINE = re.compile(r"Proxying (127\.0\.0\.1:\d+) ->")


class SubstrateError(Exception):
    pass


class NotFoundError(SubstrateError):
    pass


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
    ) -> None:
        self._atespace = atespace
        self._kubectl = kubectl
        self._context = context
        self._timeout = timeout_seconds

    @property
    def atespace(self) -> str:
        return self._atespace

    def _base(self) -> list[str]:
        base = [self._kubectl, "ate"]
        if self._context:
            base += ["--context", self._context]
        return base

    def _run(self, args: Sequence[str]) -> str:
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
            raise SubstrateError(f"{' '.join(argv[:4])}: {message}")
        return done.stdout

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
        """Only the canary proof's positive control suspends an actor; runs never do."""
        return self._json(["suspend", "actor", name, "--mode", "sync"])

    @contextlib.contextmanager
    def proxy(self, name: str, port: int) -> Iterator[str]:
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
