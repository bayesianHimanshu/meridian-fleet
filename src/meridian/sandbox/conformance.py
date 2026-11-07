"""meridian sandbox conformance: a unit test for one zone's committed sandbox policy.

A sandbox is created from the production image with the zone's policy, and shell
commands run in it with sandbox exec. Each expected refusal counts as refused only when
it fails for the right reason (the error names the target), so a command that fails for
another reason (a missing binary, a timeout) is inconclusive, never a pass:

* read_outside_allow_list  cat /etc/os-release           (a file in the image, not listed)
* write_var_tmp            : > /var/tmp/conformance       (outside read_write)
* write_opt_meridian       : > /opt/meridian/conformance  (read-only)
* egress_test_net          TCP connect to 192.0.2.1:443   (RFC 5737, connect only)
* control_tmp              write and read back /tmp/conformance (must succeed)

Writes use bash redirection, not touch: touch is not in the allow-list, so `touch X`
fails before it ever tries to write X and would prove nothing.

Then `openshell logs` is polled until the supervisor's NET:OPEN DENIED line for
192.0.2.1:443 appears or the timeout passes, and the sandbox is deleted. The check passes
only if every expected refusal was refused, the control succeeded, the log line was found
and the sandbox reported the committed policy.
"""

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import structlog

from meridian.domain.enums import Visibility
from meridian.domain.events import (
    ContainmentCheck,
    ContainmentCheckCompleted,
    EventDraft,
    PolicyDenied,
)
from meridian.platform.clock import Clock
from meridian.sandbox.logline import LogScan, NetLogRecord, scan
from meridian.sandbox.openshell import ExecOutput, OpenShellError
from meridian.sandbox.policies import POLICIES, expected_effective, policy_path, sha256_text
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker

ACTOR = "system.conformance"
TEST_NET = "192.0.2.1"
TEST_PORT = 443
DESTINATION = f"{TEST_NET}:{TEST_PORT}"
EXEC_TIMEOUT = 20


class ConformanceClient(Protocol):
    def create(
        self, name: str, image: str, policy: Path, uploads: Sequence[tuple[Path, str]]
    ) -> None: ...

    def exec(self, name: str, argv: Sequence[str], timeout_seconds: int) -> ExecOutput: ...

    def policy(self, name: str) -> str: ...

    def delete(self, name: str) -> None: ...

    def names(self) -> list[str]: ...

    def logs(self, name: str) -> str: ...


@dataclass(frozen=True, slots=True)
class CheckSpec:
    name: str
    expect: Literal["refused", "allowed"]
    command: str
    target: str  # what the refusal must name, or the control's expected output
    marker: str  # the text that proves the refusal is about the target


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "read_outside_allow_list",
        "refused",
        "cat /etc/os-release",
        "/etc/os-release",
        "/etc/os-release: Permission denied",
    ),
    CheckSpec(
        "write_var_tmp",
        "refused",
        ": > /var/tmp/conformance",
        "/var/tmp/conformance",  # noqa: S108 (a path inside the sandbox)
        "/var/tmp/conformance: Permission denied",  # noqa: S108
    ),
    CheckSpec(
        "write_opt_meridian",
        "refused",
        ": > /opt/meridian/conformance",
        "/opt/meridian/conformance",
        "/opt/meridian/conformance: Permission denied",
    ),
    CheckSpec(
        "egress_test_net",
        "refused",
        f"exec 3<>/dev/tcp/{TEST_NET}/{TEST_PORT}",
        DESTINATION,
        "connect: Permission denied",
    ),
    CheckSpec(
        "control_tmp",
        "allowed",
        "echo conformance > /tmp/conformance && cat /tmp/conformance",
        "conformance",
        "",
    ),
)
NETWORK_CHECK = "egress_test_net"


def zone_code(zone: str) -> str:
    words = zone.split("_")
    return "".join(w[0] for w in words)[:2] if len(words) > 1 else zone[:2]


def conformance_name(zone: str) -> str:
    """mer-cf-<zone code>-<8 hex>: within the gateway's 19-character limit."""
    return f"mer-cf-{zone_code(zone)}-{uuid.uuid4().hex[:8]}"


def classify(spec: CheckSpec, out: ExecOutput | None, error: str | None) -> ContainmentCheck:
    if out is None:
        outcome: Literal["refused", "allowed", "inconclusive"] = "inconclusive"
        message = error or "no output"
        exit_code = None
    else:
        exit_code = out.exit_code
        message = (out.stderr.strip() or out.stdout.strip())[-500:]
        if spec.expect == "allowed":
            ok = out.exit_code == 0 and out.stdout.strip() == spec.target
            outcome = "allowed" if ok else "inconclusive"
            if out.exit_code != 0 and "Permission denied" in out.stderr:
                outcome = "refused"
        elif out.exit_code == 0:
            outcome = "allowed"
        elif spec.marker in out.stderr:
            outcome = "refused"
        else:
            outcome = "inconclusive"
    return ContainmentCheck(
        name=spec.name,
        expect=spec.expect,
        command=spec.command,
        outcome=outcome,
        exit_code=exit_code,
        message=message,
        passed=outcome == spec.expect,
    )


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    zone: str
    sandbox: str
    image: str
    image_digest: str
    policy_file: Path
    policy_sha256: str | None
    policy_matches: bool
    checks: tuple[ContainmentCheck, ...]
    log_records: tuple[NetLogRecord, ...]  # the NET:OPEN DENIED lines for 192.0.2.1:443
    malformed_lines: tuple[str, ...]
    log_wait_ms: int

    @property
    def log_line_found(self) -> bool:
        return bool(self.log_records)

    @property
    def all_refused(self) -> bool:
        return all(c.outcome == "refused" for c in self.checks if c.expect == "refused")

    @property
    def control_ok(self) -> bool:
        return all(c.passed for c in self.checks if c.expect == "allowed")

    @property
    def passed(self) -> bool:
        return self.all_refused and self.control_ok and self.log_line_found and self.policy_matches

    def failures(self) -> list[str]:
        out = [
            f"{c.name}: expected {c.expect}, got {c.outcome} ({c.message})"
            for c in self.checks
            if not c.passed
        ]
        if not self.log_line_found:
            out.append(f"no NET:OPEN DENIED line for {DESTINATION} in openshell logs")
        if not self.policy_matches:
            out.append(f"the sandbox reported a policy that differs from {self.policy_file}")
        return out


def run_conformance(
    *,
    client: ConformanceClient,
    zone: str,
    image: str,
    image_digest: str,
    policies_dir: Path,
    tracker: SandboxTracker,
    log: structlog.typing.FilteringBoundLogger,
    log_timeout_seconds: float = 30.0,
    poll_seconds: float = 1.0,
    delete_confirm_seconds: float = 60.0,
    name_factory: Callable[[str], str] = conformance_name,
) -> ConformanceResult:
    policy = POLICIES[zone]
    policy_file = policy_path(zone, policies_dir)
    name = name_factory(zone)
    bound = log.bind(zone=zone, sandbox=name)
    deleter = BackgroundDeleter(
        delete=client.delete,
        tracker=tracker,
        workers=0,
        log=bound,
        live=client.names,
        confirm_seconds=delete_confirm_seconds,
    )
    tracker.record(name, "created")
    checks: list[ContainmentCheck] = []
    effective: str | None = None
    found: LogScan = LogScan()
    wait_ms = 0
    try:
        client.create(name, image, policy_file, [])
        effective = client.policy(name)
        for spec in CHECKS:
            try:
                out = client.exec(name, ["/usr/bin/bash", "-c", spec.command], EXEC_TIMEOUT)
            except OpenShellError as exc:
                checks.append(classify(spec, None, f"{type(exc).__name__}: {exc}"))
                continue
            checks.append(classify(spec, out, None))
        start = time.monotonic()
        while True:
            found = scan(client.logs(name), bound)
            if found.denied(DESTINATION) or time.monotonic() - start >= log_timeout_seconds:
                break
            time.sleep(poll_seconds)
        wait_ms = round((time.monotonic() - start) * 1000)
    finally:
        deleter.submit(name)
        deleter.close()
    return ConformanceResult(
        zone=zone,
        sandbox=name,
        image=image,
        image_digest=image_digest,
        policy_file=policy_file,
        policy_sha256=None if effective is None else sha256_text(effective),
        policy_matches=effective is not None
        and effective.strip() == expected_effective(policy).strip(),
        checks=tuple(checks),
        log_records=tuple(found.denied(DESTINATION)),
        malformed_lines=tuple(found.malformed),
        log_wait_ms=wait_ms,
    )


def ledger_drafts(result: ConformanceResult, clock: Clock) -> list[EventDraft]:
    """PolicyDenied for every observed refusal, then ContainmentCheckCompleted."""
    now = clock.now()
    policy = result.policy_file.as_posix()
    drafts: list[EventDraft] = []

    def draft(payload: PolicyDenied | ContainmentCheckCompleted) -> EventDraft:
        return EventDraft(
            occurred_at=now, actor=ACTOR, visibility=Visibility.BLINDED, payload=payload
        )

    specs = {s.name: s for s in CHECKS}
    for check in result.checks:
        if check.name == NETWORK_CHECK or check.outcome != "refused" or check.expect != "refused":
            continue
        drafts.append(
            draft(
                PolicyDenied(
                    agent_id=ACTOR,
                    resource=specs[check.name].target,
                    policy=policy,
                    reason=check.message or "refused",
                    source="harness",
                    zone=result.zone,
                    check=check.name,
                    sandbox=result.sandbox,
                    policy_sha256=result.policy_sha256,
                )
            )
        )
    for record in result.log_records:
        drafts.append(
            draft(
                PolicyDenied(
                    agent_id=ACTOR,
                    resource=record.destination,
                    policy=policy,
                    reason=record.reason,
                    source="openshell_log",
                    zone=result.zone,
                    check=NETWORK_CHECK,
                    sandbox=result.sandbox,
                    policy_sha256=result.policy_sha256,
                    raw_line=record.raw,
                )
            )
        )
    drafts.append(
        draft(
            ContainmentCheckCompleted(
                zone=result.zone,
                sandbox=result.sandbox,
                image=result.image,
                image_digest=result.image_digest,
                policy_sha256=result.policy_sha256,
                checks=result.checks,
                log_line_found=result.log_line_found,
                all_refused=result.all_refused,
                passed=result.passed,
            )
        )
    )
    return drafts
