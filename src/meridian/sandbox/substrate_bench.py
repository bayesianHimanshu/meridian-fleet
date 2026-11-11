"""`meridian substrate bench`: run times and memory for docs/substrate-eval.md.

The same agent on the same cut through either executor, N runs one after another or K at a
time. Nothing is written to the ledger. For OpenShell, sandboxes are deleted synchronously
so a run's time includes its teardown (the phase 2 baseline of about 8 s per run counted
5.4 s of deletion). For Substrate, each run reports its steps (stage, create, resume from
the golden snapshot, proxy, request, delete).

Memory per worker: each WorkerPool pod's cgroup on the kind node (memory.current now, and
memory.peak, the highest since the pod started), read with docker exec ... cat.
"""

import json
import statistics
import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from meridian.sandbox.executor import ExecutionError, ExecutionResult

NODE = "kind-control-plane"


@dataclass(slots=True)
class BenchRun:
    ok: bool
    wall_ms: int
    phases: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    started_at: float = 0.0  # time.time() when the run started
    actor: str | None = None
    worker_pod: str | None = None


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100) of a non-empty sequence."""
    ordered = sorted(values)
    rank = max(1, round(q / 100 * len(ordered) + 0.4999))
    return float(ordered[min(rank, len(ordered)) - 1])


def summary(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values),
        "mean": round(statistics.fmean(values), 1),
    }


def run_many(run: Callable[[], ExecutionResult], runs: int, concurrency: int = 1) -> list[BenchRun]:
    def one(_: int) -> BenchRun:
        started_at = time.time()
        start = time.perf_counter()
        try:
            result = run()
        except ExecutionError as exc:
            return BenchRun(
                False,
                round((time.perf_counter() - start) * 1000),
                error=str(exc)[:300],
                started_at=started_at,
            )
        sub = result.substrate
        return BenchRun(
            True,
            round((time.perf_counter() - start) * 1000),
            dict(result.phases_ms),
            started_at=started_at,
            actor=None if sub is None else sub.actor,
            worker_pod=None if sub is None else sub.worker_pod,
        )

    if concurrency <= 1:
        return [one(i) for i in range(runs)]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(one, range(runs)))


def each(runs: Sequence[BenchRun]) -> list[dict[str, Any]]:
    """One row per run, for finding slow steps (docs/substrate-eval.md, slow restores)."""
    return [
        {
            "ok": r.ok,
            "wall_ms": r.wall_ms,
            "started_at": round(r.started_at, 3),
            "actor": r.actor,
            "worker_pod": r.worker_pod,
            **r.phases,
        }
        for r in runs
    ]


def report(runs: Sequence[BenchRun]) -> dict[str, Any]:
    ok = [r for r in runs if r.ok]
    phases = sorted({k for r in ok for k in r.phases})
    return {
        "runs": len(runs),
        "failed": len(runs) - len(ok),
        "errors": sorted({r.error for r in runs if r.error})[:5],
        "wall_ms": summary([r.wall_ms for r in ok]),
        "phases_ms": {p: summary([r.phases[p] for r in ok if p in r.phases]) for p in phases},
    }


def _node(argv: Sequence[str]) -> str:
    full = ["docker", "exec", NODE, *argv]
    done = subprocess.run(  # noqa: S603 (fixed argv)
        full, capture_output=True, text=True, check=False, timeout=60
    )
    return done.stdout


def worker_memory(kubectl: str, context: str, namespace: str, pool: str) -> list[dict[str, Any]]:
    """memory.current and memory.peak (bytes) of each worker pod's cgroup."""
    pods = json.loads(
        subprocess.run(  # noqa: S603 (fixed argv)
            [kubectl, "--context", context, "-n", namespace, "get", "pods", "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
    )["items"]
    out = []
    for pod in pods:
        if not pod["metadata"]["name"].startswith(f"{pool}-"):
            continue
        uid = pod["metadata"]["uid"].replace("-", "_")
        found = _node(
            # kind's kubelet uses the systemd driver: kubelet.slice/kubelet-kubepods.slice/...
            ["find", "/sys/fs/cgroup", "-maxdepth", "5", "-type", "d", "-name", f"*pod{uid}.slice"]
        )
        path = found.strip().splitlines()[0] if found.strip() else ""
        values: dict[str, Any] = {"pod": pod["metadata"]["name"], "cgroup": path}
        for name in ("memory.current", "memory.peak"):
            text = _node(["cat", f"{path}/{name}"]).strip() if path else ""
            values[name] = int(text) if text.isdigit() else None
        out.append(values)
    return out


def scale_pool(kubectl: str, context: str, namespace: str, pool: str, replicas: int) -> None:
    """Patch the meridian WorkerPool's replicas and wait for the rollout."""
    base = [kubectl, "--context", context, "-n", namespace]
    subprocess.run(  # noqa: S603 (fixed argv)
        [
            *base,
            "patch",
            "workerpool",
            pool,
            "--type",
            "merge",
            "-p",
            json.dumps({"spec": {"replicas": replicas}}),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    time.sleep(2)
    subprocess.run(  # noqa: S603 (fixed argv)
        [*base, "rollout", "status", f"deployment/{pool}", "--timeout=300s"],
        check=True,
        capture_output=True,
        timeout=330,
    )


def host_memory() -> dict[str, int]:
    """MemAvailable and MemTotal of the host, in bytes (/proc/meminfo)."""
    out: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as handle:
        for line in handle:
            key, rest = line.split(":", 1)
            if key in ("MemTotal", "MemAvailable"):
                out[key] = int(rest.split()[0]) * 1024
    return out
