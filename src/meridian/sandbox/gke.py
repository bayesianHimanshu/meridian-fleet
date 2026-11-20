"""What the laptop reads from Google Cloud for the GKE environment (phase 4b step 3).

Only read-only gcloud commands, through one runner (tests replace it with recorded
answers): IAM policies (the dispatcher's Workload Identity check), the snapshot bucket's
objects (the leak check), and Cloud Logging entries over a time window (the leak check).
Nothing here creates or changes a cloud resource, and no credential is printed: gcloud
uses its own login, and the laptop never handles a token.

The dispatcher's Workload Identity binding. Under Workload Identity Federation for GKE every
Kubernetes ServiceAccount is already an IAM principal
(principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/
<project>.svc.id.goog/subject/ns/<namespace>/sa/<name>); nothing has to be created for it
to exist. The dispatcher calls no Google API (it reaches ate-api-server and atenet-router
inside the cluster; images are pulled by the node's service account), so its binding is
deliberately empty: `dispatcher_bindings` lists every grant that names it, directly, through
its namespace, or through the older `serviceAccount:<project>.svc.id.goog[ns/sa]` form and
a service account's workloadIdentityUser role, and the deploy refuses unless the list is
empty.
"""

import datetime as dt
import json
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from meridian.platform.config import GkeSettings

Gcloud = Callable[[Sequence[str], float], bytes]


class GcloudError(Exception):
    pass


def run_gcloud(binary: str = "gcloud") -> Gcloud:
    """The real runner: gcloud with --quiet, stdout returned, stderr only in errors."""

    def run(args: Sequence[str], timeout: float) -> bytes:
        try:
            done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
                [binary, *args, "--quiet"], capture_output=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GcloudError(f"gcloud {' '.join(args[:3])}: {exc}") from exc
        if done.returncode != 0:
            message = done.stderr.decode("utf-8", "replace").strip()[-600:]
            raise GcloudError(f"gcloud {' '.join(args[:3])}: {message}")
        return bytes(done.stdout)

    return run


def _json(gcloud: Gcloud, args: Sequence[str], timeout: float = 120) -> object:
    raw = gcloud([*args, "--format=json"], timeout)
    value: object = json.loads(raw or b"null")
    return value


def _list(value: object) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


# ------------------------------------------------- the dispatcher's Workload Identity


@dataclass(frozen=True, slots=True)
class Grant:
    resource: str
    role: str
    member: str


def names_dispatcher(member: str, g: GkeSettings, namespace: str, service_account: str) -> bool:
    pool = f"{g.project_id}.svc.id.goog"
    return member in (
        g.workload_principal(namespace, service_account),
        f"serviceAccount:{pool}[{namespace}/{service_account}]",
    ) or (
        member.startswith("principalSet://")
        and pool in member
        and f"/namespace/{namespace}" in member
    )


def _grants(resource: str, policy: object) -> Iterator[Grant]:
    bindings = policy.get("bindings") if isinstance(policy, dict) else None
    for binding in _list(bindings):
        for member in binding.get("members", []) or []:
            yield Grant(resource, str(binding.get("role")), str(member))


def dispatcher_bindings(
    gcloud: Gcloud, g: GkeSettings, namespace: str, service_account: str
) -> tuple[list[Grant], list[str]]:
    """(every grant naming the dispatcher's principal, the resources read). Read: the
    project's IAM policy, the snapshot bucket's and the Artifact Registry repository's,
    and every service account's (a workloadIdentityUser grant would let the dispatcher
    act as it)."""
    read: list[str] = []
    found: list[Grant] = []

    def check(resource: str, policy: object) -> None:
        read.append(resource)
        found.extend(
            gr
            for gr in _grants(resource, policy)
            if names_dispatcher(gr.member, g, namespace, service_account)
        )

    check(
        f"projects/{g.project_id}",
        _json(gcloud, ["projects", "get-iam-policy", g.project_id]),
    )
    check(
        f"gs://{g.bucket}",
        _json(gcloud, ["storage", "buckets", "get-iam-policy", f"gs://{g.bucket}"]),
    )
    check(
        f"artifactregistry/{g.ar_repo}",
        _json(
            gcloud,
            [
                "artifacts",
                "repositories",
                "get-iam-policy",
                g.ar_repo,
                f"--location={g.region}",
                f"--project={g.project_id}",
            ],
        ),
    )
    accounts = _json(gcloud, ["iam", "service-accounts", "list", f"--project={g.project_id}"])
    for account in _list(accounts):
        email = str(account["email"])
        check(
            f"serviceAccounts/{email}",
            _json(
                gcloud,
                ["iam", "service-accounts", "get-iam-policy", email, f"--project={g.project_id}"],
            ),
        )
    return found, read


# ------------------------------------------------------------ the snapshot bucket (GCS)


@dataclass(frozen=True, slots=True)
class GcsObject:
    name: str
    size: int


def bucket_objects(gcloud: Gcloud, bucket: str) -> list[GcsObject]:
    listed = _json(gcloud, ["storage", "objects", "list", f"gs://{bucket}/**"], timeout=300)
    out = []
    for item in _list(listed):
        url = str(item.get("storage_url") or item.get("url") or "")
        name = str(item.get("name") or url.split(f"gs://{bucket}/", 1)[-1])
        out.append(GcsObject(name=name, size=int(item.get("size") or 0)))
    return sorted(out, key=lambda o: o.name)


def bucket_read(gcloud: Gcloud, bucket: str, name: str) -> bytes:
    """One object's bytes as stored (gcloud storage cat: no decompressive transcoding of
    zstd; Substrate's objects are its own sparse zstd, decoded by the caller)."""
    return gcloud(["storage", "cat", f"gs://{bucket}/{name}"], 600)


# ----------------------------------------------------------------------- Cloud Logging


def window_filter(start: dt.datetime, end: dt.datetime) -> str:
    """Every entry of the project in [start, end] (UTC, RFC 3339)."""

    def stamp(t: dt.datetime) -> str:
        return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return f'timestamp>="{stamp(start)}" AND timestamp<="{stamp(end)}"'


def log_entries(
    gcloud: Gcloud,
    project: str,
    start: dt.datetime,
    end: dt.datetime,
    text: str | None = None,
) -> list[dict[str, Any]]:
    """Every Cloud Logging entry of the project over the window, the cluster's included
    (gcloud pages through them); with text, only entries holding it (a full-text search).
    The leak check passes text only for its random marker: the canary never goes into a
    filter, so it is not sent to Google or written to gcloud's own logs; entries are
    searched for it here."""
    query = window_filter(start, end) + (f' AND "{text}"' if text else "")
    entries = _json(
        gcloud,
        ["logging", "read", query, f"--project={project}", "--order=asc"],
        timeout=600,
    )
    return _list(entries)


def entry_text(entry: dict[str, Any]) -> bytes:
    """An entry as the bytes searched: its whole JSON (textPayload, jsonPayload,
    protoPayload, labels and resource alike)."""
    return json.dumps(entry, sort_keys=True, ensure_ascii=False).encode("utf-8")
