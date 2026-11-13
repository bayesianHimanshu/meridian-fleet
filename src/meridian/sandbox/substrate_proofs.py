"""`meridian substrate canary`: no run leaves its data at rest (phase 4a step 2, proof C1).

1. A unique canary string (MERIDIAN-CANARY-<32 hex>) is written into copies of the inputs:
   blinded/ae.parquet (AETERM of one row) of the cut, of the safety database snapshot and
   of the eTMF snapshot's cut, and the audit extract (DECISION_REASON of one row). Each
   copy is written uncompressed, and the canary is checked to appear verbatim in its
   bytes, so a search of any byte store finds it wherever those bytes, or the strings an
   agent loads from them, are kept.
2. N runs go through SubstrateExecutor on those copies (agents cycled over every zone).
   Each run already refuses an actor with a snapshot other than its golden one.
3. Then every store this code can reach is searched for the canary:
   * snapshot storage: every object in the rustfs bucket through its S3 API (a port
     forward to ate-system/rustfs), zstd objects decompressed;
   * the kind node's whole filesystem (worker pods, atelet's snapshot caches and scratch,
     pod logs, containerd, the rustfs and Postgres volumes), streamed out with tar, zstd
     and gzip files decompressed;
   * the host's var/ folder (tracking files, logs).
   It must not be found. The bucket must hold tags only (atespaces/<atespace>/tags/...),
   and every tag must be a template's golden tag.
4. With --positive-control, one more actor gets a canary run and is then suspended
   (MEMORY fidelity), and the bucket is searched again: the search must find the canary
   there, which shows it would have found a leak. The actor is then deleted and the
   bucket searched once more, to record whether deleting an actor removes its snapshot.
"""

import contextlib
import datetime as dt
import hashlib
import hmac
import os
import re
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from compression import zstd
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

CANARY_PREFIX = "MERIDIAN-CANARY-"
BUCKET = "ate-snapshots"
S3_KEY = "rustfsadmin"  # the kind install's rustfs credentials (ate-setup's defaults)
REGION = "us-east-1"
FORWARD_LINE = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+)")
NODE = "kind-control-plane"
NODE_EXCLUDES = ("proc", "sys", "dev", "run")


def new_canary() -> str:
    return f"{CANARY_PREFIX}{uuid.uuid4().hex}"


def plant(parquet: Path, column: str, canary: str) -> None:
    """Replace the column's value in the first row with the canary and rewrite the file
    uncompressed; then check that the canary is in the file's bytes."""
    frame = pl.read_parquet(parquet)
    if column not in frame.columns or frame.height == 0:
        raise ValueError(f"{parquet} has no rows or no column {column}")
    values = frame[column].cast(pl.String).to_list()
    values[0] = canary
    frame = frame.with_columns(pl.Series(column, values, dtype=pl.String))
    frame.write_parquet(parquet, compression="uncompressed", statistics=False)
    if canary.encode() not in parquet.read_bytes():
        raise ValueError(f"the canary is not verbatim in {parquet}")


@dataclass(frozen=True, slots=True)
class CanaryRoots:
    curated_root: Path
    safety_root: Path
    etmf_root: Path
    audit_root: Path
    planted: tuple[str, ...]


def make_copies(
    *,
    canary: str,
    dest: Path,
    study_id: str,
    cut_id: str,
    curated_root: Path,
    safety_root: Path,
    etmf_root: Path,
    audit_root: Path,
    audit_seq: int | None,
) -> CanaryRoots:
    """Copies of the cut's blinded and ops areas, the safety and eTMF snapshots of the
    cut, and one audit extract, with the canary planted in each."""
    planted: list[str] = []
    roots = {
        "curated": (curated_root, ("blinded", "ops")),
        "safety": (safety_root, ("blinded",)),
        "etmf": (etmf_root, ("blinded",)),
    }
    for label, (root, areas) in roots.items():
        for area in areas:
            source = root / study_id / cut_id / area
            target = dest / label / study_id / cut_id / area
            shutil.copytree(source, target)
        ae = dest / label / study_id / cut_id / "blinded" / "ae.parquet"
        if ae.exists():
            plant(ae, "AETERM", canary)
            planted.append(ae.relative_to(dest).as_posix())
    if audit_seq is not None:
        source = audit_root / str(audit_seq) / "audit"
        target = dest / "audit" / str(audit_seq) / "audit"
        shutil.copytree(source, target)
        extract = target / "audit_extract.parquet"
        plant(extract, "DECISION_REASON", canary)
        planted.append(extract.relative_to(dest).as_posix())
    return CanaryRoots(
        curated_root=dest / "curated",
        safety_root=dest / "safety",
        etmf_root=dest / "etmf",
        audit_root=dest / "audit",
        planted=tuple(planted),
    )


# ---------------------------------------------------------------- searching byte stores


# Substrate's sparse snapshot format (pkg/objectstorage/sparsezstd.go): the magic and a
# little-endian u32 version in the clear, then one zstd stream of the populated extents.
SPARSE_MAGIC = b"ATESPRSE"
SPARSE_HEADER = len(SPARSE_MAGIC) + 4


def _decompressed(name: str, data: bytes) -> list[bytes]:
    out = [data]
    if data[: len(SPARSE_MAGIC)] == SPARSE_MAGIC:
        with contextlib.suppress(zstd.ZstdError):
            out.append(zstd.decompress(data[SPARSE_HEADER:]))
        return out
    if name.endswith(".zstd") or name.endswith(".zst") or data[:4] == b"\x28\xb5\x2f\xfd":
        with contextlib.suppress(zstd.ZstdError):
            out.append(zstd.decompress(data))
    if name.endswith(".gz") or data[:2] == b"\x1f\x8b":
        import gzip

        with contextlib.suppress(OSError, EOFError):
            out.append(gzip.decompress(data))
    return out


@dataclass(slots=True)
class SearchReport:
    store: str
    items: int = 0
    bytes_scanned: int = 0
    decompressed: int = 0
    hits: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def scan(self, name: str, data: bytes, needle: bytes) -> None:
        self.items += 1
        for i, blob in enumerate(_decompressed(name, data)):
            self.bytes_scanned += len(blob)
            self.decompressed += i > 0
            if needle in blob:
                self.hits.append(name + (" (decompressed)" if i else ""))
                return

    def as_dict(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "items": self.items,
            "bytes_scanned": self.bytes_scanned,
            "decompressed": self.decompressed,
            "hits": self.hits,
            "errors": self.errors[:20],
        }


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class LocalS3:
    """GET and list on the kind install's rustfs, path style, AWS signature v4."""

    def __init__(self, endpoint: str, access_key: str = S3_KEY, secret_key: str = S3_KEY) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._access = access_key
        self._secret = secret_key

    def _get(self, path: str, query: dict[str, str]) -> bytes:
        now = dt.datetime.now(dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        host = urllib.parse.urlparse(self._endpoint).netloc
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
            for k, v in sorted(query.items())
        )
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_path = urllib.parse.quote(path, safe="/-_.~")
        headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        signed = "host;x-amz-content-sha256;x-amz-date"
        request = f"GET\n{canonical_path}\n{canonical_query}\n{headers}\n{signed}\n{payload_hash}"
        scope = f"{day}/{REGION}/s3/aws4_request"
        to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(request.encode()).hexdigest()}"
        )
        key = _sign(
            _sign(_sign(_sign(f"AWS4{self._secret}".encode(), day), REGION), "s3"), "aws4_request"
        )
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        auth = (
            f"AWS4-HMAC-SHA256 Credential={self._access}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        )
        url = f"{self._endpoint}{canonical_path}" + (
            f"?{canonical_query}" if canonical_query else ""
        )
        req = urllib.request.Request(  # noqa: S310 (the local rustfs)
            url,
            headers={
                "Authorization": auth,
                "x-amz-date": amz_date,
                "x-amz-content-sha256": payload_hash,
            },
        )
        with urllib.request.urlopen(req, timeout=120) as response:  # noqa: S310
            return bytes(response.read())

    def keys(self, bucket: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        token: str | None = None
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        while True:
            query = {"list-type": "2"}
            if token:
                query["continuation-token"] = token
            root = ET.fromstring(self._get(f"/{bucket}", query))  # noqa: S314
            for item in root.findall("s3:Contents", ns):
                out.append(
                    (item.findtext("s3:Key", "", ns), int(item.findtext("s3:Size", "0", ns)))
                )
            if root.findtext("s3:IsTruncated", "false", ns) != "true":
                return out
            token = root.findtext("s3:NextContinuationToken", "", ns)

    def get(self, bucket: str, key: str) -> bytes:
        return self._get(f"/{bucket}/{key}", {})


@contextlib.contextmanager
def port_forward(
    kubectl: str, context: str, namespace: str, service: str, port: int
) -> Iterator[int]:
    process = subprocess.Popen(  # noqa: S603 (fixed argv)
        [
            kubectl,
            "--context",
            context,
            "-n",
            namespace,
            "port-forward",
            f"svc/{service}",
            f"0:{port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        match = FORWARD_LINE.search(line)
        if not match:
            raise RuntimeError(f"kubectl port-forward printed {line!r}")
        yield int(match.group(1))
    finally:
        process.terminate()
        process.wait(timeout=10)


def search_bucket(s3: LocalS3, needle: bytes) -> tuple[SearchReport, list[str]]:
    report = SearchReport("rustfs bucket " + BUCKET)
    keys = s3.keys(BUCKET)
    for key, _size in keys:
        try:
            report.scan(key, s3.get(BUCKET, key), needle)
        except OSError as exc:
            report.errors.append(f"{key}: {exc}")
    return report, [k for k, _ in keys]


def search_node(needle: bytes, node: str = NODE, roots: Sequence[str] = ("/",)) -> SearchReport:
    """Stream the node's filesystem out with tar and search every regular file."""
    report = SearchReport(f"kind node {node} filesystem")
    excludes = [f"--exclude=/{d}" for d in NODE_EXCLUDES]
    argv = ["docker", "exec", node, "tar", "-cf", "-", "--ignore-failed-read", *excludes, *roots]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # noqa: S603
    assert process.stdout is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
            for member in tar:
                if not member.isreg():
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                report.scan(member.name, handle.read(), needle)
    except tarfile.TarError as exc:
        report.errors.append(f"tar stream: {exc}")
    finally:
        process.wait()
    return report


def _pod(kubectl: str, context: str, namespace: str, selector: str) -> str:
    done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
        [
            kubectl,
            "--context",
            context,
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            selector,
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if done.returncode != 0 or not done.stdout.strip():
        raise RuntimeError(f"no pod {selector} in {namespace}: {done.stderr[-300:]}")
    return done.stdout.strip()


def search_pod(
    kubectl: str, context: str, namespace: str, selector: str, needle: bytes
) -> SearchReport:
    """Stream a pod's whole filesystem out with tar (kubectl exec) and search every
    regular file, as search_node does for the node (phase 4b: the dispatcher's pod)."""
    pod = _pod(kubectl, context, namespace, selector)
    report = SearchReport(f"pod {namespace}/{pod} filesystem")
    argv = [
        kubectl,
        "--context",
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
    ]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # noqa: S603
    assert process.stdout is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
            for member in tar:
                if member.isreg() and (handle := tar.extractfile(member)) is not None:
                    report.scan(member.name, handle.read(), needle)
    except tarfile.TarError as exc:
        report.errors.append(f"tar stream: {exc}")
    finally:
        process.wait()
    if report.items == 0:
        report.errors.append("no files streamed")
    return report


def search_pod_logs(
    kubectl: str, context: str, namespace: str, selector: str, needle: bytes
) -> SearchReport:
    """Every log line the pod's containers wrote (this run of the pod and, if it
    restarted, the previous one)."""
    pod = _pod(kubectl, context, namespace, selector)
    report = SearchReport(f"pod {namespace}/{pod} logs")
    for extra in ([], ["--previous"]):
        done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
            [
                kubectl,
                "--context",
                context,
                "-n",
                namespace,
                "logs",
                pod,
                "--all-containers",
                *extra,
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if done.returncode == 0:
            report.scan("logs" + (" (previous)" if extra else ""), done.stdout, needle)
    return report


def plant_in_pod(
    kubectl: str, context: str, namespace: str, selector: str, canary: str, path: str
) -> None:
    """Positive control for search_pod: write the canary into a file in the pod."""
    pod = _pod(kubectl, context, namespace, selector)
    subprocess.run(  # noqa: S603 (argv built from fixed parts)
        [
            kubectl,
            "--context",
            context,
            "-n",
            namespace,
            "exec",
            "-i",
            pod,
            "--",
            "sh",
            "-c",
            f"cat > {path}",
        ],
        input=canary.encode(),
        check=True,
        timeout=60,
    )


def remove_in_pod(kubectl: str, context: str, namespace: str, selector: str, path: str) -> None:
    pod = _pod(kubectl, context, namespace, selector)
    subprocess.run(  # noqa: S603 (argv built from fixed parts)
        [kubectl, "--context", context, "-n", namespace, "exec", pod, "--", "rm", "-f", path],
        check=True,
        timeout=60,
    )


def search_dir(root: Path, needle: bytes, label: str) -> SearchReport:
    report = SearchReport(label)
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                report.scan(path.as_posix(), path.read_bytes(), needle)
            except OSError as exc:
                report.errors.append(f"{path}: {exc}")
    return report


def bucket_layout(keys: Sequence[str], atespace: str, golden_tags: set[str]) -> dict[str, Any]:
    """Every object must be under atespaces/<atespace>/tags/<a golden snapshot uuid>/."""
    prefix = f"atespaces/{atespace}/"
    other = [k for k in keys if not k.startswith(prefix + "tags/")]
    tags = sorted(
        {
            k[len(prefix + "tags/") :].split("/", 1)[0]
            for k in keys
            if k.startswith(prefix + "tags/")
        }
    )
    return {
        "objects": len(keys),
        "outside_tags": other,
        "tag_snapshots": tags,
        "non_golden_tags": [t for t in tags if t not in golden_tags],
    }
