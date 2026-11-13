"""`meridian substrate placement-check`: workers only on the worker nodes (phase 4b step 3).

On GKE the WorkerPool must land on the Spot pool and nothing else may: that pool's nodes
carry the label `meridian.dev/pool=substrate` and the taint
`meridian.dev/worker=true:NoSchedule` (docs/gke-plan.md 6.5), and the WorkerPool's template
names both ([substrate] worker_node_label and worker_taint). The check reads, never writes,
on GKE:

1. the WorkerPool in the cluster has that nodeSelector and toleration;
2. every worker pod runs on a node with the label;
3. every labelled node carries the taint, and the dispatcher's pod is on a node without
   the label;
4. a pod that names the label but does not tolerate the taint stays Pending with the
   scheduler's "untolerated taint" (needs a labelled node: on GKE one is up while workers
   run).

kind has one node. It carries the label for good (`label_node`), and the taint only during
this check (with --taint-node): a lasting NoSchedule taint on the only node would keep every
other pod (ate-system, the dispatcher, CoreDNS) from being scheduled again. With the taint
on, the check restarts the worker pods and shows they come back on the node, then removes
the taint.
"""

import json
import subprocess
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from meridian.platform.config import SubstrateSettings

PROBE_NAMESPACE = "meridian-placement"


class PlacementError(Exception):
    pass


def _kubectl(s: SubstrateSettings, *args: str, input_text: str | None = None) -> str:
    try:
        done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
            [s.kubectl, "--context", s.context, *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlacementError(f"kubectl {' '.join(args[:3])}: {exc}") from exc
    if done.returncode != 0:
        raise PlacementError(f"kubectl {' '.join(args[:4])}: {done.stderr.strip()[-500:]}")
    return done.stdout


def _json(s: SubstrateSettings, *args: str) -> dict[str, Any]:
    value = json.loads(_kubectl(s, *args, "-o", "json"))
    assert isinstance(value, dict)
    return value


@dataclass(slots=True)
class Placement:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append((name, ok, detail))

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)


def _labelled(s: SubstrateSettings, node: dict[str, Any]) -> bool:
    labels = dict(node.get("metadata", {}).get("labels") or {})
    return all(labels.get(k) == v for k, v in s.node_selector.items())


def _tainted(s: SubstrateSettings, node: dict[str, Any]) -> bool:
    want = s.toleration
    return any(
        t.get("key") == want["key"]
        and t.get("value") == want["value"]
        and t.get("effect") == want["effect"]
        for t in node.get("spec", {}).get("taints") or []
    )


def label_node(s: SubstrateSettings, node: str) -> str:
    """kind: the node carries the worker label for good."""
    key, value = next(iter(s.node_selector.items()))
    return _kubectl(s, "label", "node", node, f"{key}={value}", "--overwrite").strip()


def set_taint(s: SubstrateSettings, node: str, on: bool) -> None:
    taint = s.worker_taint if on else s.worker_taint.split("=", 1)[0] + "-"
    _kubectl(s, "taint", "node", node, taint)


def worker_pods(s: SubstrateSettings) -> list[dict[str, Any]]:
    """The WorkerPool's pods (the controller labels them), leaving out terminating ones."""
    pods = _json(s, "-n", s.namespace, "get", "pods", "-l", f"ate.dev/worker-pool={s.worker_pool}")
    return [p for p in pods.get("items", []) if not p.get("metadata", {}).get("deletionTimestamp")]


def untolerated_probe(s: SubstrateSettings, wait_seconds: float = 60) -> tuple[bool, str]:
    """A pod naming the worker label without the toleration must stay Pending, refused by
    the scheduler for the taint. It never runs (its image is never pulled)."""
    name = f"probe-{uuid.uuid4().hex[:8]}"
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": PROBE_NAMESPACE}}
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": PROBE_NAMESPACE},
        "spec": {
            "nodeSelector": dict(s.node_selector),
            "automountServiceAccountToken": False,
            "containers": [{"name": "p", "image": "registry.k8s.io/pause:3.10"}],
        },
    }
    _kubectl(s, "apply", "-f", "-", input_text=json.dumps(namespace))
    try:
        _kubectl(s, "apply", "-f", "-", input_text=json.dumps(pod))
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            events = _json(
                s, "-n", PROBE_NAMESPACE, "get", "events",
                f"--field-selector=involvedObject.name={name}",
            )  # fmt: skip
            for e in events.get("items", []):
                message = str(e.get("message", ""))
                if e.get("reason") == "FailedScheduling" and "untolerated taint" in message:
                    phase = _json(s, "-n", PROBE_NAMESPACE, "get", "pod", name)["status"]["phase"]
                    return phase == "Pending", message[:300]
            time.sleep(2)
        return False, "no FailedScheduling event for the taint"
    finally:
        _kubectl(s, "delete", "namespace", PROBE_NAMESPACE, "--wait=false")


def check(s: SubstrateSettings, taint_node: bool, dispatcher_selector: str) -> Placement:
    result = Placement()
    pool = _json(s, "-n", s.namespace, "get", "workerpool", s.worker_pool)
    template = dict(pool.get("spec", {}).get("template") or {})
    result.add(
        "workerpool_selector",
        dict(template.get("nodeSelector") or {}) == s.node_selector,
        json.dumps(template.get("nodeSelector")),
    )
    result.add(
        "workerpool_toleration",
        s.toleration in list(template.get("tolerations") or []),
        json.dumps(template.get("tolerations")),
    )
    nodes = {n["metadata"]["name"]: n for n in _json(s, "get", "nodes").get("items", [])}
    labelled = sorted(n for n, v in nodes.items() if _labelled(s, v))
    result.add("labelled_nodes", bool(labelled), ", ".join(labelled) or "none")
    tainted_here: list[str] = []
    try:
        if taint_node:
            if s.target != "kind":
                raise PlacementError("--taint-node is for kind; on GKE the node pool is tainted")
            for n in labelled:
                set_taint(s, n, on=True)
                tainted_here.append(n)
            nodes = {n["metadata"]["name"]: n for n in _json(s, "get", "nodes").get("items", [])}
            _kubectl(s, "-n", s.namespace, "rollout", "restart", f"deployment/{s.worker_pool}")
            _kubectl(
                s, "-n", s.namespace, "rollout", "status", f"deployment/{s.worker_pool}",
                "--timeout=300s",
            )  # fmt: skip
        untainted = [n for n in labelled if not _tainted(s, nodes[n])]
        result.add("labelled_nodes_tainted", not untainted, f"untainted: {untainted or 'none'}")
        pods = worker_pods(s)
        placed = [
            (
                p["metadata"]["name"],
                p.get("spec", {}).get("nodeName"),
                p.get("status", {}).get("phase"),
            )
            for p in pods
        ]
        result.add(
            "workers_on_labelled_nodes",
            bool(placed) and all(n in labelled and ph == "Running" for _, n, ph in placed),
            "; ".join(f"{name} on {n} ({ph})" for name, n, ph in placed) or "no worker pods",
        )
        dispatcher = _json(
            s, "-n", s.dispatcher_namespace, "get", "pods", "-l", dispatcher_selector
        )
        hosts = [p.get("spec", {}).get("nodeName") for p in dispatcher.get("items", [])]
        if s.target == "gke":
            result.add(
                "dispatcher_off_worker_nodes",
                bool(hosts) and not set(hosts) & set(labelled),
                f"dispatcher on {hosts}",
            )
        if labelled and all(_tainted(s, nodes[n]) for n in labelled):
            ok, detail = untolerated_probe(s)
            result.add("untolerated_pod_refused", ok, detail)
    finally:
        for n in tainted_here:
            set_taint(s, n, on=False)
    return result


def summary(lines: Sequence[tuple[str, bool, str]]) -> list[str]:
    return [f"{'pass' if ok else 'FAIL'}  {name:<28} {detail[:160]}" for name, ok, detail in lines]
