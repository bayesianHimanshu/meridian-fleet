"""Parse the OpenShell 0.1.7 supervisor's network log lines from `openshell logs`.

Pinned to the 0.1.7 format, taken from real lines (one line, wrapped here):

    [1794055103.727] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED /usr/bin/bash(0)
        -> 192.0.2.1:443 [reason:transparent_tcp_policy_denied]

an allowed connection, which names the policy that allowed it instead of a reason:

    [1794067456.128] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] ALLOWED
        /usr/local/bin/python3.14(0) -> api.anthropic.com:443
        [policy:_provider_meridian_anthropic engine:opa]

and the supervisor's own connections, which carry no disposition:

    [1794050707.375] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] 127.0.0.1:17670

Only NET:OPEN lines are parsed. Any line that mentions "NET:OPEN" but does not match the
pattern is malformed: it is counted and logged, never dropped silently and never raised.
Every other line is counted as other.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

NET_MARKER = "NET:OPEN"
_NET = re.compile(
    r"^\[(?P<ts>\d+(?:\.\d+)?)\] \[sandbox\] \[OCSF \] \[ocsf\] NET:OPEN "
    r"\[(?P<severity>[A-Z]+)\] (?P<disposition>[A-Z]+) "
    r"(?P<binary>\S+)\((?P<pid>\d+)\) -> (?P<host>[^\s:\[\]]+):(?P<port>\d{1,5}) "
    r"\[(?:reason:(?P<reason>[^\]\s]+)"
    r"|policy:(?P<policy>[^\]\s]+) engine:(?P<engine>[^\]\s]+))\]$"
)
_SUPERVISOR = re.compile(
    r"^\[(?P<ts>\d+(?:\.\d+)?)\] \[sandbox\] \[OCSF \] \[ocsf\] NET:OPEN "
    r"\[(?P<severity>[A-Z]+)\] (?P<host>[^\s:\[\]]+):(?P<port>\d{1,5})$"
)


@dataclass(frozen=True, slots=True)
class NetLogRecord:
    at: datetime
    severity: str
    disposition: str  # DENIED, ALLOWED, or SUPERVISOR for the supervisor's own opens
    binary: str
    host: str
    port: int
    reason: str  # why it was denied; empty when allowed
    raw: str
    policy: str = ""  # the policy rule that allowed it; empty when denied

    @property
    def denied(self) -> bool:
        return self.disposition == "DENIED"

    @property
    def destination(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(slots=True)
class LogScan:
    records: list[NetLogRecord] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)
    other: int = 0

    def denied(self, destination: str) -> list[NetLogRecord]:
        return [r for r in self.records if r.denied and r.destination == destination]


def parse_line(line: str) -> NetLogRecord | None:
    """The record, or None when the line is not a well-formed NET:OPEN line."""
    raw = line.rstrip("\r\n")
    match = _NET.match(raw)
    if match is not None:
        disposition, binary = match["disposition"], match["binary"]
        reason, policy = match["reason"] or "", match["policy"] or ""
    else:
        match = _SUPERVISOR.match(raw)
        if match is None:
            return None
        disposition, binary, reason, policy = "SUPERVISOR", "", "", ""
    port = int(match["port"])
    if not 0 < port < 65536:
        return None
    return NetLogRecord(
        at=datetime.fromtimestamp(float(match["ts"]), tz=UTC),
        severity=match["severity"],
        disposition=disposition,
        binary=binary,
        host=match["host"],
        port=port,
        reason=reason,
        raw=raw,
        policy=policy,
    )


def scan(text: str, log: structlog.typing.FilteringBoundLogger | None = None) -> LogScan:
    out = LogScan()
    for line in text.splitlines():
        if NET_MARKER not in line:
            out.other += 1
            continue
        try:
            record = parse_line(line)
        except ValueError, OverflowError:  # for example an out-of-range timestamp
            record = None
        if record is None:
            out.malformed.append(line)
            if log is not None:
                log.warning("openshell_log.malformed_line", line=line[:500])
            continue
        out.records.append(record)
    return out
