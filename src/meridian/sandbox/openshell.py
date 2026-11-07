"""A thin wrapper around the openshell CLI (checked against openshell 0.1.7).

Only the commands this project is allowed to run: create, exec, get (policy), list and
delete, on sandboxes this code names. Tests inject a fake with the same protocol.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

OPENSHELL = "openshell"
# Seconds the host waits beyond the in-sandbox timeout before giving up on the CLI.
CLI_GRACE_SECONDS = 30


class OpenShellError(Exception):
    pass


class ExecTimeoutError(OpenShellError):
    pass


@dataclass(frozen=True, slots=True)
class ExecOutput:
    exit_code: int
    stdout: str
    stderr: str


class OpenShellClient(Protocol):
    def create(
        self, name: str, image: str, policy: Path, uploads: Sequence[tuple[Path, str]]
    ) -> None: ...

    def exec(self, name: str, argv: Sequence[str], timeout_seconds: int) -> ExecOutput: ...

    def policy(self, name: str) -> str: ...

    def delete(self, name: str) -> None: ...

    def names(self) -> list[str]: ...


class SubprocessOpenShellClient:
    """Runs the installed openshell CLI. Never touches the gateway or global settings."""

    def __init__(self, binary: str = OPENSHELL, timeout_seconds: int = 300) -> None:
        self._binary = binary
        self._timeout = timeout_seconds

    def _run(self, args: Sequence[str], timeout: float | None = None) -> ExecOutput:
        try:
            done = subprocess.run(  # noqa: S603 (argv built here, no shell)
                [self._binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout or self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecTimeoutError(f"openshell {args[0]} {args[1]} timed out") from exc
        except OSError as exc:
            raise OpenShellError(f"cannot run {self._binary}: {exc}") from exc
        return ExecOutput(done.returncode, done.stdout, done.stderr)

    def _checked(self, args: Sequence[str]) -> str:
        out = self._run(args)
        if out.exit_code != 0:
            tail = (out.stderr or out.stdout).strip()[-2000:]
            raise OpenShellError(f"openshell {' '.join(args[:2])} exited {out.exit_code}: {tail}")
        return out.stdout

    def create(
        self, name: str, image: str, policy: Path, uploads: Sequence[tuple[Path, str]]
    ) -> None:
        args = [
            "sandbox",
            "create",
            "--name",
            name,
            "--from",
            image,
            "--policy",
            str(policy),
            "--detach",
            "--no-auto-providers",
            "--no-tty",
        ]
        for local, remote in uploads:
            args += ["--upload", f"{local}:{remote}"]
        if uploads:
            # The staging folder is outside any git work tree; say so instead of relying
            # on the CLI's warning, and never let .gitignore drop a staged file.
            args.append("--no-git-ignore")
        self._checked(args)

    def exec(self, name: str, argv: Sequence[str], timeout_seconds: int) -> ExecOutput:
        args = [
            "sandbox",
            "exec",
            "--name",
            name,
            "--no-tty",
            "--no-login-shell",
            "--timeout",
            str(timeout_seconds),
            "--",
            *argv,
        ]
        return self._run(args, timeout=timeout_seconds + CLI_GRACE_SECONDS)

    def policy(self, name: str) -> str:
        return self._checked(["sandbox", "get", name, "--policy-only"])

    def delete(self, name: str) -> None:
        self._checked(["sandbox", "delete", name])

    def names(self) -> list[str]:
        return self._checked(["sandbox", "list", "--names"]).split()
