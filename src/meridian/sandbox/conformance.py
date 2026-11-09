"""meridian sandbox conformance: a unit test for one committed sandbox policy.

A sandbox is created from the production image with the policy (and, for an agent policy,
its providers attached), and commands run in it with sandbox exec. Each expected refusal
counts as refused only when it fails for the right reason (the error names the target), so
a command that fails for another reason (a missing binary, a timeout) is inconclusive,
never a pass.

Zone policies (no egress):

* read_outside_allow_list  cat /etc/os-release           (a file in the image, not listed)
* write_var_tmp            : > /var/tmp/conformance       (outside read_write)
* write_opt_meridian       : > /opt/meridian/conformance  (read-only)
* egress_test_net          TCP connect to 192.0.2.1:443   (RFC 5737, connect only)
* control_tmp              write and read back /tmp/conformance (must succeed)

rbqm.narrator (the meridian-anthropic provider attached):

* anthropic_models         python GET https://api.anthropic.com/v1/models, status 200
                           (the proxy substituted the credential at the allowed endpoint)
* egress_example_com       python GET https://example.com (refused)
* the two writes, egress_test_net and control_tmp as above. read_outside_allow_list is
  left out: with any network rule 0.1.7 adds /etc (and more) to the read-only paths, which
  expected_effective pins and docs/threat-model.md records.
* Whether ANTHROPIC_API_KEY inside the sandbox starts with "sk-ant-" (it must not): the
  sandbox prints only True or False, and only that is recorded.

safety.local_model (no provider; Ollama on the host address only):

* ollama_tags              python GET http://172.17.0.1:11434/api/tags, status 200 (the
                           proxy reaches Ollama from the host namespace, past ufw)
* ollama_other_endpoint    python GET /api/version on the same address: the proxy answers
                           403 policy_denied (only GET /api/tags and POST /api/chat pass)
* egress_anthropic         python GET https://api.anthropic.com/v1/models (refused)
* egress_example_com, the two writes, egress_test_net and control_tmp as above.

Every policy also gets one check per staged area (/stage/<area>): opening an area the
policy lists must succeed (read_stage_<area>, allowed), and opening any other must be
refused by Landlock (read_stage_<area>, refused). So a blinded zone is shown unable to
read /stage/unblinded, and the local model zone unable to read any cut area.

Writes use bash redirection, not touch: touch is not in the allow-list, so `touch X`
fails before it ever tries to write X and would prove nothing.

Then `openshell logs` is polled until the supervisor's NET:OPEN DENIED line for
192.0.2.1:443 appears or the timeout passes, and the sandbox is deleted. The check passes
only if every expected refusal was refused, every expected success succeeded, the log line
was found, the sandbox reported the committed policy, and (with a provider) the key inside
the sandbox was a placeholder.
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
from meridian.sandbox.policies import (
    ALL_POLICIES,
    LOCAL_MODEL_POLICY,
    NARRATOR_POLICY,
    OLLAMA_HOST,
    OLLAMA_PORT,
    STAGE_AREAS,
    expected_effective,
    policy_path,
    sha256_text,
    stage_path,
)
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker

ACTOR = "system.conformance"
TEST_NET = "192.0.2.1"
TEST_PORT = 443
DESTINATION = f"{TEST_NET}:{TEST_PORT}"
EXEC_TIMEOUT = 30
PYTHON = "/opt/meridian/venv/bin/python"


class ConformanceClient(Protocol):
    def create(
        self,
        name: str,
        image: str,
        policy: Path,
        uploads: Sequence[tuple[Path, str]],
        providers: Sequence[str] = (),
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
    target: str  # what the refusal must name, or the expected stdout of a success
    marker: str  # the text that proves the refusal is about the target


READ_OUTSIDE = CheckSpec(
    "read_outside_allow_list",
    "refused",
    "cat /etc/os-release",
    "/etc/os-release",
    "/etc/os-release: Permission denied",
)
WRITE_VAR_TMP = CheckSpec(
    "write_var_tmp",
    "refused",
    ": > /var/tmp/conformance",
    "/var/tmp/conformance",  # noqa: S108 (a path inside the sandbox)
    "/var/tmp/conformance: Permission denied",  # noqa: S108
)
WRITE_OPT = CheckSpec(
    "write_opt_meridian",
    "refused",
    ": > /opt/meridian/conformance",
    "/opt/meridian/conformance",
    "/opt/meridian/conformance: Permission denied",
)
EGRESS_TEST_NET = CheckSpec(
    "egress_test_net",
    "refused",
    f"exec 3<>/dev/tcp/{TEST_NET}/{TEST_PORT}",
    DESTINATION,
    "connect: Permission denied",
)
CONTROL_TMP = CheckSpec(
    "control_tmp",
    "allowed",
    "echo conformance > /tmp/conformance && cat /tmp/conformance",
    "conformance",
    "",
)


def _python_get(url: str, headers: str = "{}") -> str:
    """A bash command: python GETs url and prints 'status N' (exit 0 only on 200), or
    exits 1 with 'ErrorType: message' on stderr. Never prints the response body."""
    return (
        f'{PYTHON} -c "import httpx, os, sys\n'
        f"try:\n"
        f"    r = httpx.get('{url}', headers={headers}, timeout=20)\n"
        f"except httpx.HTTPError as e:\n"
        f"    sys.exit(type(e).__name__ + ': ' + str(e))\n"
        f"print('status', r.status_code)\n"
        f'sys.exit(r.status_code != 200)"'
    )


ANTHROPIC_MODELS = CheckSpec(
    "anthropic_models",
    "allowed",
    _python_get(
        "https://api.anthropic.com/v1/models",
        "{'x-api-key': os.environ.get('ANTHROPIC_API_KEY', ''), 'anthropic-version': '2023-06-01'}",
    ),
    "status 200",
    "",
)
EGRESS_EXAMPLE = CheckSpec(
    "egress_example_com",
    "refused",
    _python_get("https://example.com"),
    "example.com:443",
    "ConnectError: [Errno 13] Permission denied",
)
EGRESS_ANTHROPIC = CheckSpec(
    "egress_anthropic",
    "refused",
    _python_get("https://api.anthropic.com/v1/models"),
    "api.anthropic.com:443",
    "ConnectError: [Errno 13] Permission denied",
)
OLLAMA_URL = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}"
OLLAMA_TAGS_CHECK = CheckSpec(
    "ollama_tags",
    "allowed",
    _python_get(f"{OLLAMA_URL}/api/tags"),
    "status 200",
    "",
)
# The proxy's L7 refusal is an HTTP 403 with error policy_denied; the command turns it
# into a non-zero exit with that text on stderr. Any other status is inconclusive.
OLLAMA_OTHER_ENDPOINT = CheckSpec(
    "ollama_other_endpoint",
    "refused",
    (
        f'{PYTHON} -c "import httpx, sys\n'
        f"r = httpx.get('{OLLAMA_URL}/api/version', timeout=20)\n"
        f"if r.status_code == 403 and 'policy_denied' in r.text:\n"
        f"    sys.exit('HTTP 403 policy_denied: GET /api/version')\n"
        f"print('status', r.status_code)\n"
        f'sys.exit(r.status_code != 200)"'
    ),
    f"{OLLAMA_URL}/api/version",
    "HTTP 403 policy_denied",
)
# Prints True or False only: never the value.
KEY_PREFIX_COMMAND = (
    f"{PYTHON} -c \"import os; print(os.environ.get('ANTHROPIC_API_KEY', '')"
    ".startswith('sk-ant-'))\""
)

ZONE_CHECKS: tuple[CheckSpec, ...] = (
    READ_OUTSIDE,
    WRITE_VAR_TMP,
    WRITE_OPT,
    EGRESS_TEST_NET,
    CONTROL_TMP,
)
NARRATOR_CHECKS: tuple[CheckSpec, ...] = (
    ANTHROPIC_MODELS,
    EGRESS_EXAMPLE,
    WRITE_VAR_TMP,
    WRITE_OPT,
    EGRESS_TEST_NET,
    CONTROL_TMP,
)
LOCAL_MODEL_CHECKS: tuple[CheckSpec, ...] = (
    OLLAMA_TAGS_CHECK,
    OLLAMA_OTHER_ENDPOINT,
    EGRESS_ANTHROPIC,
    EGRESS_EXAMPLE,
    WRITE_VAR_TMP,
    WRITE_OPT,
    EGRESS_TEST_NET,
    CONTROL_TMP,
)
CHECKS = ZONE_CHECKS  # the zone checks (step 2b name)
NETWORK_CHECK = EGRESS_TEST_NET.name
# Network refusals are recorded from the supervisor's log line, not the harness.
NETWORK_CHECKS = frozenset({EGRESS_TEST_NET.name, EGRESS_EXAMPLE.name, EGRESS_ANTHROPIC.name})


def stage_check(area: str, readable: bool) -> CheckSpec:
    """Open /stage/<area> as a directory: allowed when the policy lists it, else refused."""
    path = stage_path(area)
    if readable:
        return CheckSpec(
            f"read_stage_{area}", "allowed", f"exec 3< {path} && echo readable", "readable", ""
        )
    return CheckSpec(
        f"read_stage_{area}", "refused", f"exec 3< {path}", path, f"{path}: Permission denied"
    )


def checks_for(policy_name: str) -> tuple[CheckSpec, ...]:
    base = {NARRATOR_POLICY: NARRATOR_CHECKS, LOCAL_MODEL_POLICY: LOCAL_MODEL_CHECKS}.get(
        policy_name, ZONE_CHECKS
    )
    reads = ALL_POLICIES[policy_name].reads
    return (*base, *(stage_check(a, a in reads) for a in STAGE_AREAS))


def zone_code(zone: str) -> str:
    """Two letters. An agent policy (zone.agent) takes the initials of both parts."""
    if "." in zone:
        a, b = zone.split(".", 1)
        return a[0] + b[0]
    words = zone.split("_")
    return "".join(w[0] for w in words)[:2] if len(words) > 1 else zone[:2]


def conformance_name(zone: str) -> str:
    """mer-cf-<code>-<8 hex>: within the gateway's 19-character limit."""
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


def parse_key_prefix(out: ExecOutput | None) -> bool | None:
    """True or False from the sandbox; None if it printed anything else."""
    if out is None or out.exit_code != 0:
        return None
    return {"True": True, "False": False}.get(out.stdout.strip())


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    zone: str  # the policy name: a zone, or an agent id with its own policy
    sandbox: str
    image: str
    image_digest: str
    policy_file: Path
    policy_sha256: str | None
    policy_matches: bool
    checks: tuple[ContainmentCheck, ...]
    log_records: tuple[NetLogRecord, ...]  # every NET:OPEN DENIED line in the log
    malformed_lines: tuple[str, ...]
    log_wait_ms: int
    providers: tuple[str, ...] = ()
    allowed_records: tuple[NetLogRecord, ...] = ()  # NET:OPEN ALLOWED lines, for display
    # With a provider: does ANTHROPIC_API_KEY in the sandbox start with "sk-ant-"? None
    # without a provider, or if the sandbox printed anything but True or False.
    credential_value_visible: bool | None = None

    @property
    def log_line_found(self) -> bool:
        return any(r.destination == DESTINATION for r in self.log_records)

    @property
    def all_refused(self) -> bool:
        return all(c.outcome == "refused" for c in self.checks if c.expect == "refused")

    @property
    def control_ok(self) -> bool:
        return all(c.passed for c in self.checks if c.expect == "allowed")

    @property
    def credential_ok(self) -> bool:
        return not self.providers or self.credential_value_visible is False

    @property
    def passed(self) -> bool:
        return (
            self.all_refused
            and self.control_ok
            and self.log_line_found
            and self.policy_matches
            and self.credential_ok
        )

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
        if not self.credential_ok:
            out.append(
                "ANTHROPIC_API_KEY in the sandbox "
                + (
                    "starts with sk-ant- (a real key)"
                    if self.credential_value_visible
                    else "could not be checked"
                )
            )
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
    """zone is any committed policy name: a zone, or rbqm.narrator."""
    policy = ALL_POLICIES[zone]
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
    key_visible: bool | None = None
    try:
        client.create(name, image, policy_file, [], providers=policy.providers)
        effective = client.policy(name)
        for spec in checks_for(zone):
            try:
                out = client.exec(name, ["/usr/bin/bash", "-c", spec.command], EXEC_TIMEOUT)
            except OpenShellError as exc:
                checks.append(classify(spec, None, f"{type(exc).__name__}: {exc}"))
                continue
            checks.append(classify(spec, out, None))
        if policy.providers:
            try:
                key_out: ExecOutput | None = client.exec(
                    name, ["/usr/bin/bash", "-c", KEY_PREFIX_COMMAND], EXEC_TIMEOUT
                )
            except OpenShellError:
                key_out = None
            key_visible = parse_key_prefix(key_out)
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
        log_records=tuple(r for r in found.records if r.denied),
        malformed_lines=tuple(found.malformed),
        log_wait_ms=wait_ms,
        providers=policy.providers,
        allowed_records=tuple(r for r in found.records if r.disposition == "ALLOWED"),
        credential_value_visible=key_visible,
    )


def _log_check(record: NetLogRecord) -> str | None:
    if record.destination == DESTINATION:
        return EGRESS_TEST_NET.name
    if record.host == "example.com" or record.host.endswith(".example.com"):
        return EGRESS_EXAMPLE.name
    if record.host == "api.anthropic.com":
        return EGRESS_ANTHROPIC.name
    return None


def ledger_drafts(result: ConformanceResult, clock: Clock) -> list[EventDraft]:
    """PolicyDenied for every observed refusal, then ContainmentCheckCompleted."""
    now = clock.now()
    policy = result.policy_file.as_posix()
    zone = ALL_POLICIES[result.zone].zone
    agent_policy = result.zone if result.zone != zone else None
    drafts: list[EventDraft] = []

    def draft(payload: PolicyDenied | ContainmentCheckCompleted) -> EventDraft:
        return EventDraft(
            occurred_at=now, actor=ACTOR, visibility=Visibility.BLINDED, payload=payload
        )

    specs = {s.name: s for s in checks_for(result.zone)}
    for check in result.checks:
        if check.name in NETWORK_CHECKS or check.outcome != "refused" or check.expect != "refused":
            continue
        drafts.append(
            draft(
                PolicyDenied(
                    agent_id=ACTOR,
                    resource=specs[check.name].target,
                    policy=policy,
                    reason=check.message or "refused",
                    source="harness",
                    zone=zone,
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
                    reason=record.reason or "denied",
                    source="openshell_log",
                    zone=zone,
                    check=_log_check(record),
                    sandbox=result.sandbox,
                    policy_sha256=result.policy_sha256,
                    raw_line=record.raw,
                )
            )
        )
    drafts.append(
        draft(
            ContainmentCheckCompleted(
                zone=zone,
                sandbox=result.sandbox,
                image=result.image,
                image_digest=result.image_digest,
                policy_sha256=result.policy_sha256,
                checks=result.checks,
                log_line_found=result.log_line_found,
                all_refused=result.all_refused,
                passed=result.passed,
                policy_name=agent_policy,
                providers=result.providers,
                credential_value_visible=result.credential_value_visible,
            )
        )
    )
    return drafts
