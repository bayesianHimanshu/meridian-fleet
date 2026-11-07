"""The OpenShell 0.1.7 NET:OPEN log line parser, on real lines."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from meridian.sandbox.logline import parse_line, scan

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "openshell_0_1_7_sandbox_logs.txt"
DENIED_BASH = (
    "[1794050714.254] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED /usr/bin/bash(0) -> "
    "192.0.2.1:443 [reason:transparent_tcp_policy_denied]"
)


def test_a_real_denied_line() -> None:
    record = parse_line(DENIED_BASH)
    assert record is not None
    assert record.denied
    assert (record.binary, record.host, record.port) == ("/usr/bin/bash", "192.0.2.1", 443)
    assert record.reason == "transparent_tcp_policy_denied"
    assert record.severity == "MED"
    assert record.at == datetime.fromtimestamp(1794050714.254, tz=UTC)
    assert record.raw == DENIED_BASH


def test_the_fixture_of_real_lines() -> None:
    result = scan(FIXTURE.read_text())
    assert result.malformed == []
    assert [(r.disposition, r.destination) for r in result.records] == [
        ("SUPERVISOR", "127.0.0.1:17670"),
        ("DENIED", "192.0.2.1:443"),
        ("DENIED", "151.101.0.223:443"),
    ]
    assert result.other == 9
    assert [r.raw for r in result.denied("192.0.2.1:443")] == [DENIED_BASH]
    assert result.denied("151.101.0.223:443")[0].binary == "/usr/local/bin/python3.14"
    # The proxy's WARN line names the same destination but is not the OCSF record.
    assert all("[OCSF ]" in r.raw for r in result.records)


PREFIX = "[1794050714.254] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED "


@pytest.mark.parametrize(
    "line",
    [
        PREFIX + "/usr/bin/bash(0) -> 192.0.2.1:443",  # no reason
        PREFIX + "/usr/bin/bash -> 192.0.2.1:443 [reason:x]",  # no pid
        PREFIX + "/usr/bin/bash(0) -> 192.0.2.1:99999 [reason:x]",  # port out of range
        "[99999999999999999999] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] 127.0.0.1:17670",
        "NET:OPEN",
        "garbage NET:OPEN \x00\xff",
    ],
)
def test_malformed_net_lines_are_counted_and_logged_never_raised(line: str) -> None:
    with capture_logs() as logs:
        result = scan(f"{DENIED_BASH}\n{line}\nan unrelated line\n", structlog.get_logger())
    assert result.malformed == [line]
    assert len(result.records) == 1
    assert result.other == 1
    assert [e["event"] for e in logs] == ["openshell_log.malformed_line"]


def test_empty_and_unrelated_text() -> None:
    assert scan("").records == []
    result = scan("one\ntwo\n")
    assert (result.records, result.malformed, result.other) == ([], [], 2)
