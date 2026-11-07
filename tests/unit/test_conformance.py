"""meridian sandbox conformance against a fake OpenShell client."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from meridian.domain.enums import EventType
from meridian.domain.events import ContainmentCheckCompleted, Event, PolicyDenied
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.sandbox import policies
from meridian.sandbox.conformance import (
    ANTHROPIC_MODELS,
    CHECKS,
    EGRESS_EXAMPLE,
    KEY_PREFIX_COMMAND,
    NARRATOR_CHECKS,
    ConformanceResult,
    conformance_name,
    ledger_drafts,
    parse_key_prefix,
    run_conformance,
)
from meridian.sandbox.openshell import ExecOutput, ExecTimeoutError
from meridian.sandbox.tracking import SandboxTracker
from tests.rbqm_support import NOW
from tests.support import make_ledger

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "openshell_0_1_7_sandbox_logs.txt"
DENIAL = (
    "[1794055316.838] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED /usr/bin/bash(0) -> "
    "192.0.2.1:443 [reason:transparent_tcp_policy_denied]"
)
# Real 0.1.7 lines without any denied or allowed connection: what a fresh sandbox's log
# holds before the checks run.
BACKGROUND = "".join(
    line
    for line in FIXTURE.read_text().splitlines(keepends=True)
    if "DENIED" not in line and "ALLOWED" not in line
)
GOOD: dict[str, ExecOutput] = {
    "cat /etc/os-release": ExecOutput(1, "", "cat: /etc/os-release: Permission denied\n"),
    ": > /var/tmp/conformance": ExecOutput(
        1, "", "/usr/bin/bash: line 1: /var/tmp/conformance: Permission denied\n"
    ),
    ": > /opt/meridian/conformance": ExecOutput(
        1, "", "/usr/bin/bash: line 1: /opt/meridian/conformance: Permission denied\n"
    ),
    "exec 3<>/dev/tcp/192.0.2.1/443": ExecOutput(
        1,
        "",
        "/usr/bin/bash: connect: Permission denied\n"
        "/usr/bin/bash: line 1: /dev/tcp/192.0.2.1/443: Permission denied\n",
    ),
    "echo conformance > /tmp/conformance && cat /tmp/conformance": ExecOutput(
        0, "conformance\n", ""
    ),
}


@dataclass
class FakeClient:
    outputs: dict[str, ExecOutput] = field(default_factory=lambda: dict(GOOD))
    log_texts: list[str] = field(default_factory=lambda: [BACKGROUND + DENIAL + "\n"])
    effective: str = policies.expected_effective(policies.POLICIES["rbqm"])
    raise_on: str | None = None
    live: set[str] = field(default_factory=set)
    created: list[str] = field(default_factory=list)
    providers: list[tuple[str, ...]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    log_calls: int = 0

    def create(
        self,
        name: str,
        image: str,
        policy: Path,
        uploads: Sequence[tuple[Path, str]],
        providers: Sequence[str] = (),
    ) -> None:
        assert uploads == []
        self.created.append(name)
        self.providers.append(tuple(providers))
        self.live.add(name)

    def exec(self, name: str, argv: Sequence[str], timeout_seconds: int) -> ExecOutput:
        assert list(argv[:2]) == ["/usr/bin/bash", "-c"]
        if argv[2] == self.raise_on:
            raise ExecTimeoutError("openshell sandbox exec timed out")
        return self.outputs[argv[2]]

    def policy(self, name: str) -> str:
        return self.effective

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.live.discard(name)

    def names(self) -> list[str]:
        return sorted(self.live)

    def logs(self, name: str) -> str:
        self.log_calls += 1
        index = min(self.log_calls, len(self.log_texts)) - 1
        return self.log_texts[index]


def _run(
    client: FakeClient, tmp_path: Path, timeout: float = 5.0, zone: str = "rbqm"
) -> ConformanceResult:
    return run_conformance(
        client=client,
        zone=zone,
        image="meridian-agent:test",
        image_digest="sha256:" + "a" * 64,
        policies_dir=REPO / "policies",
        tracker=SandboxTracker(tmp_path / "sandboxes.jsonl", FixedClock(NOW)),
        log=structlog.get_logger(),
        log_timeout_seconds=timeout,
        poll_seconds=0.01,
        delete_confirm_seconds=1.0,
    )


def _check(result: ConformanceResult, name: str) -> str:
    return next(c.outcome for c in result.checks if c.name == name)


def test_all_refused_control_ok_and_log_line_found_passes(tmp_path: Path) -> None:
    client = FakeClient()
    result = _run(client, tmp_path)
    assert result.passed, result.failures()
    assert result.all_refused
    assert result.control_ok
    assert [r.raw for r in result.log_records] == [DENIAL]
    assert result.policy_sha256 == policies.sha256_text(client.effective)
    assert [c.name for c in result.checks] == [s.name for s in CHECKS]
    assert client.deleted == client.created
    assert result.malformed_lines == ()


def test_a_missing_refusal_fails(tmp_path: Path) -> None:
    client = FakeClient()
    client.outputs[": > /var/tmp/conformance"] = ExecOutput(0, "", "")
    result = _run(client, tmp_path)
    assert not result.passed
    assert not result.all_refused
    assert _check(result, "write_var_tmp") == "allowed"
    assert any("write_var_tmp" in f for f in result.failures())
    assert client.deleted == client.created


def test_a_refusal_for_the_wrong_reason_is_inconclusive(tmp_path: Path) -> None:
    """touch is not in the allow-list: refusing the binary proves nothing about the path."""
    client = FakeClient()
    client.outputs[": > /opt/meridian/conformance"] = ExecOutput(
        126, "", "/usr/bin/bash: line 1: /usr/bin/touch: Permission denied\n"
    )
    result = _run(client, tmp_path)
    assert _check(result, "write_opt_meridian") == "inconclusive"
    assert not result.passed


def test_a_failed_control_fails(tmp_path: Path) -> None:
    client = FakeClient()
    client.outputs["echo conformance > /tmp/conformance && cat /tmp/conformance"] = ExecOutput(
        1, "", "/usr/bin/bash: line 1: /tmp/conformance: Permission denied\n"
    )
    result = _run(client, tmp_path)
    assert result.all_refused
    assert not result.control_ok
    assert not result.passed


def test_the_log_line_missing_until_the_timeout_fails(tmp_path: Path) -> None:
    client = FakeClient(log_texts=[BACKGROUND])
    result = _run(client, tmp_path, timeout=0.05)
    assert result.all_refused
    assert not result.log_line_found
    assert not result.passed
    assert client.log_calls >= 2  # it polled
    assert "no NET:OPEN DENIED line" in " ".join(result.failures())
    assert client.deleted == client.created


def test_a_log_line_that_arrives_late_is_found(tmp_path: Path) -> None:
    early = BACKGROUND
    client = FakeClient(log_texts=[early, early, early + DENIAL + "\n"])
    result = _run(client, tmp_path)
    assert result.passed
    assert client.log_calls == 3


def test_an_exec_error_is_inconclusive_and_still_deletes(tmp_path: Path) -> None:
    client = FakeClient(raise_on="cat /etc/os-release")
    result = _run(client, tmp_path)
    assert _check(result, "read_outside_allow_list") == "inconclusive"
    assert not result.passed
    assert client.deleted == client.created


def test_a_different_effective_policy_fails(tmp_path: Path) -> None:
    client = FakeClient(effective="version: 1\nfilesystem_policy: {}")
    result = _run(client, tmp_path)
    assert not result.policy_matches
    assert not result.passed


def test_the_sandbox_is_tracked_and_deleted(tmp_path: Path) -> None:
    client = FakeClient()
    result = _run(client, tmp_path)
    tracker = SandboxTracker(tmp_path / "sandboxes.jsonl", FixedClock(NOW))
    assert tracker.latest() == {result.sandbox: "deleted"}


def test_names_fit_the_gateway_limit() -> None:
    for zone in policies.ALL_POLICIES:
        assert len(conformance_name(zone)) <= 19


def _drafts_in_a_ledger(result: ConformanceResult, tmp_path: Path) -> list[Event]:
    ledger = make_ledger(tmp_path / "l.sqlite3", FixedClock(NOW))
    events = [ledger.append(d) for d in ledger_drafts(result, FixedClock(NOW))]
    assert verify(ledger).ok
    ledger.close()
    return events


def test_ledger_records_harness_and_openshell_log_denials(tmp_path: Path) -> None:
    result = _run(FakeClient(), tmp_path)
    events = _drafts_in_a_ledger(result, tmp_path)
    denials = [e.payload for e in events if isinstance(e.payload, PolicyDenied)]
    assert [(d.check, d.source) for d in denials] == [
        ("read_outside_allow_list", "harness"),
        ("write_var_tmp", "harness"),
        ("write_opt_meridian", "harness"),
        ("egress_test_net", "openshell_log"),
    ]
    network = denials[-1]
    assert network.raw_line == DENIAL
    assert network.resource == "192.0.2.1:443"
    assert network.reason == "transparent_tcp_policy_denied"
    assert all(d.raw_line is None for d in denials[:3])
    assert denials[0].resource == "/etc/os-release"
    assert all(d.zone == "rbqm" and d.policy_sha256 == result.policy_sha256 for d in denials)
    (done,) = [e.payload for e in events if isinstance(e.payload, ContainmentCheckCompleted)]
    assert events[-1].payload == done
    assert events[-1].event_type is EventType.CONTAINMENT_CHECK_COMPLETED
    assert (done.all_refused, done.log_line_found, done.passed) == (True, True, True)
    assert done.checks == result.checks


def test_a_failed_check_is_still_recorded(tmp_path: Path) -> None:
    client = FakeClient(log_texts=[""])
    client.outputs[": > /var/tmp/conformance"] = ExecOutput(0, "", "")
    result = _run(client, tmp_path, timeout=0.02)
    events = _drafts_in_a_ledger(result, tmp_path)
    denials = [e.payload for e in events if isinstance(e.payload, PolicyDenied)]
    assert [d.check for d in denials] == ["read_outside_allow_list", "write_opt_meridian"]
    done = events[-1].payload
    assert isinstance(done, ContainmentCheckCompleted)
    assert (done.all_refused, done.log_line_found, done.passed) == (False, False, False)


EXAMPLE_DENIAL = (
    "[1794067439.225] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED "
    "/usr/local/bin/python3.14(0) -> example.com:443 [reason:transparent_tcp_policy_denied]"
)
ALLOWED_LINE = (
    "[1794067456.128] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] ALLOWED "
    "/usr/local/bin/python3.14(0) -> api.anthropic.com:443 "
    "[policy:_provider_meridian_anthropic engine:opa]"
)


def _narrator_client(key_prefix: str = "False") -> FakeClient:
    outputs = dict(GOOD)
    outputs[ANTHROPIC_MODELS.command] = ExecOutput(0, "status 200\n", "")
    outputs[EGRESS_EXAMPLE.command] = ExecOutput(
        1, "", "ConnectError: [Errno 13] Permission denied\n"
    )
    outputs[KEY_PREFIX_COMMAND] = ExecOutput(0, key_prefix + "\n", "")
    return FakeClient(
        outputs=outputs,
        log_texts=[BACKGROUND + ALLOWED_LINE + "\n" + EXAMPLE_DENIAL + "\n" + DENIAL + "\n"],
        effective=policies.expected_effective(policies.ALL_POLICIES["rbqm.narrator"]),
    )


def test_the_narrator_policy_passes_with_its_provider_attached(tmp_path: Path) -> None:
    client = _narrator_client()
    result = _run(client, tmp_path, zone="rbqm.narrator")
    assert result.passed, result.failures()
    assert client.providers == [("meridian-anthropic",)]
    assert [c.name for c in result.checks] == [s.name for s in NARRATOR_CHECKS]
    assert result.credential_value_visible is False
    assert [r.destination for r in result.log_records] == ["example.com:443", "192.0.2.1:443"]
    assert [r.policy for r in result.allowed_records] == ["_provider_meridian_anthropic"]
    assert result.sandbox.startswith("mer-cf-rn-")


def test_zone_policies_attach_no_provider(tmp_path: Path) -> None:
    client = FakeClient()
    result = _run(client, tmp_path)
    assert client.providers == [()]
    assert result.credential_value_visible is None
    assert result.credential_ok


def test_a_real_key_inside_the_sandbox_fails(tmp_path: Path) -> None:
    result = _run(_narrator_client("True"), tmp_path, zone="rbqm.narrator")
    assert result.credential_value_visible is True
    assert not result.passed
    assert any("starts with sk-ant-" in f for f in result.failures())


def test_an_unreadable_key_check_fails(tmp_path: Path) -> None:
    result = _run(_narrator_client("maybe"), tmp_path, zone="rbqm.narrator")
    assert result.credential_value_visible is None
    assert not result.passed


def test_a_401_from_the_models_endpoint_fails(tmp_path: Path) -> None:
    client = _narrator_client()
    client.outputs[ANTHROPIC_MODELS.command] = ExecOutput(1, "status 401\n", "")
    result = _run(client, tmp_path, zone="rbqm.narrator")
    assert _check(result, "anthropic_models") == "inconclusive"
    assert "status 401" in " ".join(result.failures())
    assert not result.passed


def test_example_com_reachable_fails(tmp_path: Path) -> None:
    client = _narrator_client()
    client.outputs[EGRESS_EXAMPLE.command] = ExecOutput(0, "status 200\n", "")
    result = _run(client, tmp_path, zone="rbqm.narrator")
    assert _check(result, "egress_example_com") == "allowed"
    assert not result.passed


def test_the_key_check_prints_only_true_or_false() -> None:
    # The one print is of the boolean startswith(...), never of the value.
    assert KEY_PREFIX_COMMAND.count("print(") == 1
    assert "print(os.environ.get('ANTHROPIC_API_KEY', '').startswith('sk-ant-'))" in (
        KEY_PREFIX_COMMAND
    )
    assert parse_key_prefix(ExecOutput(0, "sk-ant-abc\n", "")) is None


def test_narrator_ledger_records(tmp_path: Path) -> None:
    result = _run(_narrator_client(), tmp_path, zone="rbqm.narrator")
    events = _drafts_in_a_ledger(result, tmp_path)
    denials = [e.payload for e in events if isinstance(e.payload, PolicyDenied)]
    assert [(d.check, d.source) for d in denials] == [
        ("write_var_tmp", "harness"),
        ("write_opt_meridian", "harness"),
        ("egress_example_com", "openshell_log"),
        ("egress_test_net", "openshell_log"),
    ]
    assert denials[2].raw_line == EXAMPLE_DENIAL
    assert all(d.zone == "rbqm" for d in denials)
    assert all(d.policy == "policies/rbqm.narrator.yaml" for d in denials) or all(
        d.policy.endswith("rbqm.narrator.yaml") for d in denials
    )
    done = events[-1].payload
    assert isinstance(done, ContainmentCheckCompleted)
    assert (done.zone, done.policy_name, done.providers) == (
        "rbqm",
        "rbqm.narrator",
        ("meridian-anthropic",),
    )
    assert done.credential_value_visible is False
    assert done.passed
