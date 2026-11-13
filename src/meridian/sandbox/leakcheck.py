"""`meridian leakcheck`: no run leaves its data at rest, on kind or GKE (phase 4b step 3).

The successor of `meridian substrate canary` (phase 4a step 2) for runs that go through
the dispatcher. A unique canary (MERIDIAN-CANARY-<32 hex>) is planted in copies of the
inputs, N runs of the chosen zones go through the dispatcher on those copies, and every
store the check can reach is searched for it (meridian.sandbox.leakscan: as stored and
decompressed). The canary must be found nowhere.

Every source also has a positive control, something the same search must find there, and
the check fails if a control is missing (a search that finds nothing proves nothing):

* snapshot bucket: every object (kind: rustfs over S3; GKE: GCS with gcloud storage).
  Control: every golden snapshot decodes to the actor's memory, which holds the worker's
  path (WORKER_MARKER).
* Cloud Logging (GKE): every entry of the project over the run window. Control: the
  marker, sent to the dispatcher as a request path (it logs each request's path).
* the dispatcher pod: its whole filesystem (tar through kubectl exec). Control: the
  marker, written to its /tmp.
* the dispatcher's logs. Control: the marker, as for Cloud Logging.
* every node's filesystem: a scan pod on each node (the host's / read-only, root with
  only CAP_DAC_READ_SEARCH) searches in place and returns a report. Control: the marker,
  written into the scan pod's own writable layer, which lies on the node's disk.
* the laptop's var/: every file. Control: the marker, in a file written there.

The marker (MERIDIAN-PC-<32 hex>) is a separate random string, never the canary; the
canary leaves the laptop only inside the runs' inputs and the needles sent to the scan
pods on stdin. The bucket must also hold only golden tags (bucket_layout).
"""

import contextlib
import datetime as dt
import json
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meridian.sandbox import gke
from meridian.sandbox import substrate_proofs as sp
from meridian.sandbox.leakscan import Report, scan_tree
from meridian.sandbox.substrate_templates import WORKER_BIN

MARKER_PREFIX = "MERIDIAN-PC-"
WORKER_MARKER = WORKER_BIN  # in every golden snapshot's memory (the actor's argv)
CANARY, MARKER, WORKER = 0, 1, 2  # needle indices
NAMESPACE = "meridian-leakcheck"
SCAN_LABEL = "meridian-leakcheck"
POD_CONTROL_PATH = "/tmp/meridian-leakcheck-control"  # noqa: S108 (inside the pod)
NODE_CONTROL_PATH = "/var/tmp/meridian-leakcheck-control"  # noqa: S108 (the scan pod's layer)
PYTHON = "/opt/meridian/venv/bin/python"


def new_marker() -> str:
    return f"{MARKER_PREFIX}{uuid.uuid4().hex}"


@dataclass(slots=True)
class Source:
    """One store's search, its positive control and the verdict."""

    name: str
    report: Report
    control: str
    control_found: bool
    control_where: list[str] = field(default_factory=list)

    @property
    def canary_hits(self) -> list[str]:
        return self.report.hits.get(CANARY, [])

    @property
    def passed(self) -> bool:
        return not self.canary_hits and self.control_found

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "passed": self.passed,
            "canary_hits": self.canary_hits,
            "positive_control": self.control,
            "control_found": self.control_found,
            "control_where": self.control_where[:10],
            "search": self.report.as_dict(),
        }


def _marker_source(name: str, report: Report, control: str) -> Source:
    where = report.hits.get(MARKER, [])
    return Source(name, report, control, bool(where), where)


# -------------------------------------------------------------------------- the bucket


def golden_control(report: Report, keys: Sequence[str], atespace: str, golden: set[str]) -> Source:
    """Every golden tag in the bucket must have decoded to the worker's path."""
    prefix = f"atespaces/{atespace}/tags/"
    tags = {k[len(prefix) :].split("/", 1)[0] for k in keys if k.startswith(prefix)}
    hit_tags = {
        h[len(prefix) :].split("/", 1)[0]
        for h in report.hits.get(WORKER, [])
        if h.startswith(prefix) and h.endswith("(decompressed)")
    }
    wanted = tags & golden
    missing = sorted(wanted - hit_tags)
    return Source(
        "snapshot bucket",
        report,
        f"each of the {len(wanted)} golden snapshots decodes to memory holding {WORKER_MARKER}",
        bool(wanted) and not missing,
        [f"missing in {t}" for t in missing] or sorted(hit_tags),
    )


def search_rustfs(kubectl: str, context: str, needles: Sequence[bytes]) -> tuple[Report, list[str]]:
    report = Report(f"rustfs bucket {sp.BUCKET} (kind)")
    with sp.port_forward(kubectl, context, "ate-system", "rustfs", 9000) as port:
        s3 = sp.LocalS3(f"http://127.0.0.1:{port}")
        keys = [k for k, _ in s3.keys(sp.BUCKET)]
        for key in keys:
            try:
                report.scan_bytes(key, s3.get(sp.BUCKET, key), needles)
            except OSError as exc:
                report.errors.append(f"{key}: {exc}")
    return report, keys


def search_gcs(
    gcloud: gke.Gcloud, bucket: str, needles: Sequence[bytes]
) -> tuple[Report, list[str]]:
    report = Report(f"gs://{bucket} (GKE)")
    objects = gke.bucket_objects(gcloud, bucket)
    for obj in objects:
        try:
            report.scan_bytes(obj.name, gke.bucket_read(gcloud, bucket, obj.name), needles)
        except gke.GcloudError as exc:
            report.errors.append(f"{obj.name}: {exc}"[:300])
    return report, [o.name for o in objects]


# ----------------------------------------------------------------------- Cloud Logging


def search_cloud_logging(
    gcloud: gke.Gcloud,
    project: str,
    start: dt.datetime,
    end: dt.datetime,
    marker: str,
    needles: Sequence[bytes],
    wait_seconds: float = 300,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Source:
    """Wait until the marker has been ingested (it may be sent to Google in a filter: it
    is random and not data), then read every entry of the window and search them here
    (the canary is never put in a filter)."""
    deadline = clock() + wait_seconds
    while True:
        polled = gke.log_entries(gcloud, project, start, end, text=marker)
        seen = [e for e in polled if marker.encode() in gke.entry_text(e)]
        if seen or clock() >= deadline:
            break
        sleep(15)
    report = Report(f"Cloud Logging, project {project}, {gke.window_filter(start, end)}")
    for i, entry in enumerate(gke.log_entries(gcloud, project, start, end)):
        name = f"{entry.get('logName', '?')} {entry.get('insertId', i)}"
        report.scan_bytes(name, gke.entry_text(entry), needles)
    source = _marker_source(
        "Cloud Logging", report, "the marker, logged by the dispatcher as a request path"
    )
    if not seen:
        source.report.errors.append(f"the marker was not ingested within {wait_seconds:.0f} s")
    return source


# ----------------------------------------------------------------- the dispatcher's pod


def _kubectl(kubectl: str, context: str, *args: str) -> list[str]:
    return [kubectl, "--context", context, *args]


def search_pod_tree(
    kubectl: str, context: str, namespace: str, pod: str, needles: Sequence[bytes]
) -> Report:
    """A pod's whole filesystem, streamed out with tar (kubectl exec)."""
    report = Report(f"pod {namespace}/{pod} filesystem")
    argv = _kubectl(
        kubectl,
        context,
        "-n",
        namespace,
        "exec",
        pod,
        "--",
        "tar",
        "-cf",
        "-",
        "--ignore-failed-read",
        "--exclude=/proc",
        "--exclude=/sys",
        "--exclude=/dev",
        "/",
    )
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # noqa: S603
    assert process.stdout is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
            for member in tar:
                if member.isreg() and (handle := tar.extractfile(member)) is not None:
                    report.scan_bytes(member.name, handle.read(), needles)
    except tarfile.TarError as exc:
        report.errors.append(f"tar stream: {exc}")
    finally:
        process.wait()
    if report.items == 0:
        report.errors.append("no files streamed")
    return report


def search_pod_logs(
    kubectl: str, context: str, namespace: str, pod: str, needles: Sequence[bytes]
) -> Report:
    report = Report(f"pod {namespace}/{pod} logs")
    for extra in ([], ["--previous"]):
        done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
            _kubectl(kubectl, context, "-n", namespace, "logs", pod, "--all-containers", *extra),
            capture_output=True,
            timeout=120,
            check=False,
        )
        if done.returncode == 0:
            report.scan_bytes("logs" + (" (previous)" if extra else ""), done.stdout, needles)
    return report


def write_in_pod(
    kubectl: str, context: str, namespace: str, pod: str, path: str, text: str | None
) -> None:
    """Write text to path in the pod through stdin (never argv), or remove it (None)."""
    command = f"cat > {path}" if text is not None else f"rm -f {path}"
    subprocess.run(  # noqa: S603 (argv built from fixed parts)
        _kubectl(kubectl, context, "-n", namespace, "exec", "-i", pod, "--", "sh", "-c", command),
        input=(text or "").encode(),
        check=True,
        capture_output=True,
        timeout=60,
    )


def pod_name(kubectl: str, context: str, namespace: str, selector: str) -> str:
    done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
        _kubectl(
            kubectl,
            context,
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            selector,
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if done.returncode != 0 or not done.stdout.strip():
        raise RuntimeError(f"no running pod {selector} in {namespace}: {done.stderr[-300:]}")
    return done.stdout.strip()


# ------------------------------------------------------------------- node filesystems


def scan_pod_manifest(name: str, node: str, image: str) -> dict[str, Any]:
    """A pod pinned to one node that can read the node's filesystem and nothing more: the
    host's / mounted read-only, root with only CAP_DAC_READ_SEARCH (read any file), no
    ServiceAccount token, no service links, tolerating every taint (worker nodes)."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"workload": SCAN_LABEL}},
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "tolerations": [{"operator": "Exists"}],
            "containers": [
                {
                    "name": "scan",
                    "image": image,
                    "command": ["sleep", "7200"],
                    "securityContext": {
                        "runAsUser": 0,
                        "runAsGroup": 0,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"], "add": ["DAC_READ_SEARCH"]},
                    },
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"memory": "1Gi"},
                    },
                    "volumeMounts": [{"name": "host", "mountPath": "/host", "readOnly": True}],
                }
            ],
            "volumes": [{"name": "host", "hostPath": {"path": "/", "type": "Directory"}}],
        },
    }


def nodes(kubectl: str, context: str) -> list[str]:
    done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
        _kubectl(kubectl, context, "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return done.stdout.split()


@contextlib.contextmanager
def scan_namespace(kubectl: str, context: str) -> Iterator[None]:
    manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": NAMESPACE, "labels": {"workload": SCAN_LABEL}},
    }
    subprocess.run(  # noqa: S603 (argv built from fixed parts)
        _kubectl(kubectl, context, "apply", "-f", "-"),
        input=json.dumps(manifest).encode(),
        check=True,
        capture_output=True,
        timeout=60,
    )
    try:
        yield
    finally:
        subprocess.run(  # noqa: S603 (argv built from fixed parts)
            _kubectl(kubectl, context, "delete", "namespace", NAMESPACE, "--wait=true"),
            capture_output=True,
            timeout=300,
            check=False,
        )


def search_node(
    kubectl: str,
    context: str,
    node: str,
    image: str,
    canary: str,
    marker: str,
    timeout: float = 3600,
) -> Source:
    """Scan one node from a pod on it; the marker goes into the pod's own writable layer
    first, so the scan must find it on the node's disk (under containerd's snapshots)."""
    name = f"scan-{uuid.uuid4().hex[:8]}"
    ns = ["-n", NAMESPACE]
    subprocess.run(  # noqa: S603 (argv built from fixed parts)
        _kubectl(kubectl, context, "apply", "-f", "-"),
        input=json.dumps(scan_pod_manifest(name, node, image)).encode(),
        check=True,
        capture_output=True,
        timeout=60,
    )
    try:
        subprocess.run(  # noqa: S603 (argv built from fixed parts)
            _kubectl(
                kubectl,
                context,
                *ns,
                "wait",
                "--for=condition=Ready",
                f"pod/{name}",
                "--timeout=600s",
            ),
            check=True,
            capture_output=True,
            timeout=660,
        )
        write_in_pod(kubectl, context, NAMESPACE, name, NODE_CONTROL_PATH, marker)
        done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
            _kubectl(
                kubectl,
                context,
                *ns,
                "exec",
                "-i",
                name,
                "--",
                PYTHON,
                "-m",
                "meridian.sandbox.leakscan",
                "--root",
                "/host",
            ),
            input=f"{canary}\n{marker}\n".encode(),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    finally:
        subprocess.run(  # noqa: S603 (argv built from fixed parts)
            _kubectl(kubectl, context, *ns, "delete", "pod", name, "--wait=false"),
            capture_output=True,
            timeout=60,
            check=False,
        )
    report = Report(f"node {node} filesystem (scan pod {name})")
    if done.returncode != 0:
        report.errors.append(done.stderr.decode("utf-8", "replace")[-500:])
    else:
        raw = json.loads(done.stdout)
        report.items = int(raw["items"])
        report.bytes_scanned = int(raw["bytes_scanned"])
        report.decompressed = int(raw["decompressed"])
        report.hits = {int(k): list(v) for k, v in dict(raw["hits"]).items()}
        report.errors = list(raw["errors"])
    return _marker_source(
        f"node {node}", report, f"the marker in the scan pod's own layer ({NODE_CONTROL_PATH})"
    )


# -------------------------------------------------------------------- the laptop's var/


def search_host_dir(root: Path, needles: Sequence[bytes], marker: str) -> Source:
    control = root / "leakcheck" / f"control-{uuid.uuid4().hex[:8]}"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(marker, encoding="utf-8")
    try:
        report = scan_tree(root, needles)
    finally:
        control.unlink(missing_ok=True)
    report.store = f"host {root}/"
    return _marker_source(f"host {root}/", report, f"the marker in {control}")


# ---------------------------------------------------------------------------- verdict


@dataclass(slots=True)
class LeakCheck:
    env: str
    zones: list[str]
    canary_sha256: str
    runs: list[dict[str, Any]] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    layout: dict[str, Any] = field(default_factory=dict)
    started: str = ""
    finished: str = ""

    @property
    def passed(self) -> bool:
        return (
            bool(self.sources)
            and all(s.passed for s in self.sources)
            and not self.layout.get("outside_tags")
            and not self.layout.get("non_golden_tags")
        )

    def failures(self) -> list[str]:
        out = []
        for s in self.sources:
            if s.canary_hits:
                out.append(f"{s.name}: the canary was found in {s.canary_hits[:5]}")
            if not s.control_found:
                out.append(f"{s.name}: positive control not found ({s.control})")
        for key in ("outside_tags", "non_golden_tags"):
            if self.layout.get(key):
                out.append(f"bucket: {key} {self.layout[key][:5]}")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "zones": self.zones,
            "canary_sha256": self.canary_sha256,
            "passed": self.passed,
            "failures": self.failures(),
            "started": self.started,
            "finished": self.finished,
            "runs": self.runs,
            "layout": self.layout,
            "sources": [s.as_dict() for s in self.sources],
        }
