"""meridian policy prove: every committed policy checked against its boundary.

    openshell-prover check <zone policy> --boundary <boundary> --output json

A zone passes only when the result is within_boundary and the coverage domains include
filesystem, process and landlock. Anything else (exceeds_boundary, unsupported, an error,
output that is not JSON) fails, with the prover's reason.

On 0.1.7 the prover proves filesystem containment only by matching paths against the
boundary's allow-list; the policy render test stays the primary filesystem check.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian.sandbox.policies import POLICY_BOUNDARY, boundary_path, policy_path

PROVER = "openshell-prover"
REQUIRED_DOMAINS = frozenset({"filesystem", "process", "landlock"})


class ProverError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProverResult:
    candidate: Path
    boundary: Path
    result: str
    domains: tuple[str, ...]
    reason_code: str | None
    reason: str | None
    counterexample: dict[str, Any] | None
    raw: str

    @property
    def passed(self) -> bool:
        return self.result == "within_boundary" and set(self.domains) >= REQUIRED_DOMAINS

    def explain(self) -> str:
        if self.passed:
            return f"within_boundary (coverage: {', '.join(self.domains)})"
        missing = sorted(REQUIRED_DOMAINS - set(self.domains))
        parts = [self.result]
        if self.reason_code:
            parts.append(self.reason_code)
        if self.reason:
            parts.append(self.reason)
        if self.counterexample:
            parts.append(f"counterexample {json.dumps(self.counterexample, sort_keys=True)}")
        if missing:
            parts.append(f"coverage lacks {', '.join(missing)}")
        return ": ".join(parts)


def check(
    candidate: Path, boundary: Path, binary: str = PROVER, timeout: str = "10s"
) -> ProverResult:
    try:
        done = subprocess.run(  # noqa: S603 (argv built here, no shell)
            [
                binary,
                "check",
                str(candidate),
                "--boundary",
                str(boundary),
                "--output",
                "json",
                "--timeout",
                timeout,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProverError(f"cannot run {binary}: {exc}") from exc
    try:
        data = json.loads(done.stdout)
    except ValueError:
        tail = (done.stdout + done.stderr).strip()[-500:]
        return ProverResult(candidate, boundary, "error", (), None, tail, None, done.stdout)
    return ProverResult(
        candidate=candidate,
        boundary=boundary,
        result=str(data.get("result")),
        domains=tuple(str(d) for d in (data.get("coverage") or {}).get("domains", [])),
        reason_code=data.get("reason_code"),
        reason=data.get("reason"),
        counterexample=data.get("counterexample"),
        raw=done.stdout,
    )


def prove_all(root: Path, binary: str = PROVER) -> dict[str, ProverResult]:
    return {
        zone: check(policy_path(zone, root), boundary_path(boundary, root), binary)
        for zone, boundary in sorted(POLICY_BOUNDARY.items())
    }
