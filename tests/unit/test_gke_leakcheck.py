"""The GKE reads of the leak check and the dispatcher's Workload Identity check (phase 4b
step 3), against gcloud answers recorded in the shape gcloud prints them: these sources
can only run for real on GKE."""

import datetime as dt
import gzip
import json
import struct
from collections.abc import Sequence
from compression import zstd

import pytest

from meridian.platform.config import GkeSettings, SubstrateSettings
from meridian.sandbox import gke
from meridian.sandbox import leakcheck as lc
from meridian.sandbox.leakscan import SPARSE_MAGIC, Report
from meridian.sandbox.substrate_proofs import bucket_layout

G = GkeSettings()
CANARY = "MERIDIAN-CANARY-" + "a" * 32
MARKER = "MERIDIAN-PC-" + "b" * 32
NEEDLES = [CANARY.encode(), MARKER.encode(), lc.WORKER_MARKER.encode()]
START = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)
END = dt.datetime(2026, 11, 20, 10, 30, tzinfo=dt.UTC)
DISPATCHER = G.workload_principal("meridian-dispatch", "dispatcher")


class FakeGcloud:
    """Answers by the first words of the command; records every call."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> bytes:
        self.calls.append(list(args))
        for prefix, answer in self.answers.items():
            if " ".join(args).startswith(prefix):
                value = answer(args) if callable(answer) else answer
                return value if isinstance(value, bytes) else json.dumps(value).encode()
        raise gke.GcloudError(f"no recorded answer for {' '.join(args)}")


def _policy(*bindings: tuple[str, list[str]]) -> dict[str, object]:
    return {"bindings": [{"role": r, "members": m} for r, m in bindings], "etag": "BwX"}


def _iam(project: dict[str, object], sa_policy: dict[str, object] | None = None) -> FakeGcloud:
    return FakeGcloud(
        {
            f"projects get-iam-policy {G.project_id}": project,
            f"storage buckets get-iam-policy gs://{G.bucket}": _policy(
                (
                    "roles/storage.objectAdmin",
                    [G.workload_principal("ate-system", "ate-api-server")],
                ),
            ),
            f"artifacts repositories get-iam-policy {G.ar_repo}": _policy(
                (
                    "roles/artifactregistry.reader",
                    [f"serviceAccount:ate-gke-node@{G.project_id}.iam.gserviceaccount.com"],
                )
            ),
            "iam service-accounts list": [
                {"email": f"ate-gke-node@{G.project_id}.iam.gserviceaccount.com"}
            ],
            "iam service-accounts get-iam-policy": sa_policy or {},
        }
    )


def test_the_dispatcher_holds_no_role_when_nothing_names_it() -> None:
    gcloud = _iam(
        _policy(
            (
                "roles/container.defaultNodeServiceAccount",
                [f"serviceAccount:ate-gke-node@{G.project_id}.iam.gserviceaccount.com"],
            )
        )
    )
    grants, read = gke.dispatcher_bindings(gcloud, G, "meridian-dispatch", "dispatcher")
    assert grants == []
    assert read == [
        f"projects/{G.project_id}",
        f"gs://{G.bucket}",
        f"artifactregistry/{G.ar_repo}",
        f"serviceAccounts/ate-gke-node@{G.project_id}.iam.gserviceaccount.com",
    ]
    assert all("--format=json" in call for call in gcloud.calls)
    assert not any(c[0] in ("add-iam-policy-binding", "create", "delete") for c in gcloud.calls)


@pytest.mark.parametrize(
    "member",
    [
        DISPATCHER,
        f"serviceAccount:{G.project_id}.svc.id.goog[meridian-dispatch/dispatcher]",
        f"principalSet://iam.googleapis.com/projects/{G.project_number}/locations/global/"
        f"workloadIdentityPools/{G.project_id}.svc.id.goog/namespace/meridian-dispatch",
    ],
)
def test_any_grant_that_names_the_dispatcher_is_reported(member: str) -> None:
    gcloud = _iam(
        _policy(("roles/viewer", ["user:someone@example.com"])),
        sa_policy=_policy(("roles/iam.workloadIdentityUser", [member])),
    )
    grants, _ = gke.dispatcher_bindings(gcloud, G, "meridian-dispatch", "dispatcher")
    assert [(g.role, g.member) for g in grants] == [("roles/iam.workloadIdentityUser", member)]
    assert grants[0].resource.startswith("serviceAccounts/")


def test_a_principal_of_another_namespace_is_not_the_dispatcher() -> None:
    other = G.workload_principal("ate-system", "dispatcher")
    assert not gke.names_dispatcher(other, G, "meridian-dispatch", "dispatcher")


def _sparse(payload: bytes) -> bytes:
    return SPARSE_MAGIC + struct.pack("<I", 1) + zstd.compress(payload)


TAGS = ("11111111-aaaa", "22222222-bbbb")


def _bucket(objects: dict[str, bytes]) -> FakeGcloud:
    listing = [
        {"name": k, "size": len(v), "storage_url": f"gs://{G.bucket}/{k}"}
        for k, v in objects.items()
    ]

    def cat(args: Sequence[str]) -> bytes:
        return objects[args[2].removeprefix(f"gs://{G.bucket}/")]

    return FakeGcloud({"storage objects list": listing, "storage cat": cat})


def _golden_objects(extra: bytes = b"") -> dict[str, bytes]:
    memory = b"\0" * 1000 + lc.WORKER_MARKER.encode() + b"\0serve\0--areas\0" + extra
    return {
        f"atespaces/meridian/tags/{tag}/{name}": _sparse(memory)
        for tag in TAGS
        for name in ("checkpoint.img.zstd", "pages.img.zstd")
    }


def test_the_gcs_search_decodes_every_object_and_the_golden_control_passes() -> None:
    report, keys = lc.search_gcs(_bucket(_golden_objects()), G.bucket, NEEDLES)
    source = lc.golden_control(report, keys, "meridian", set(TAGS))
    assert source.passed
    assert report.items == 4
    assert report.decompressed == 4
    assert source.canary_hits == []
    layout = bucket_layout(keys, "meridian", set(TAGS))
    assert layout["outside_tags"] == layout["non_golden_tags"] == []


def test_a_canary_in_a_snapshot_is_found_after_decoding() -> None:
    report, keys = lc.search_gcs(_bucket(_golden_objects(extra=CANARY.encode())), G.bucket, NEEDLES)
    source = lc.golden_control(report, keys, "meridian", set(TAGS))
    assert not source.passed
    assert all(h.endswith("(decompressed)") for h in source.canary_hits)
    assert len(source.canary_hits) == 4


def test_the_golden_control_fails_when_a_golden_snapshot_does_not_decode() -> None:
    objects = _golden_objects()
    objects[f"atespaces/meridian/tags/{TAGS[1]}/pages.img.zstd"] = b"not a snapshot"
    objects[f"atespaces/meridian/tags/{TAGS[1]}/checkpoint.img.zstd"] = gzip.compress(b"x")
    report, keys = lc.search_gcs(_bucket(objects), G.bucket, NEEDLES)
    source = lc.golden_control(report, keys, "meridian", set(TAGS))
    assert not source.control_found
    assert source.control_where == [f"missing in {TAGS[1]}"]


def _entry(i: int, text: str) -> dict[str, object]:
    return {
        "insertId": f"id{i}",
        "logName": f"projects/{G.project_id}/logs/stdout",
        "resource": {"type": "k8s_container", "labels": {"cluster_name": G.cluster}},
        "jsonPayload": {"event": "dispatcher.request", "path": text},
        "timestamp": "2026-11-20T10:05:00Z",
    }


def test_cloud_logging_waits_for_the_marker_then_searches_every_entry_locally() -> None:
    polls = {"n": 0}
    entries = [_entry(1, "/v1/runs"), _entry(2, f"/v1/templates/{MARKER}")]

    def read(args: Sequence[str]) -> bytes:
        query = args[2]
        assert CANARY not in query  # the canary is never sent in a filter
        if MARKER in query:
            polls["n"] += 1
            return json.dumps([] if polls["n"] < 3 else entries[1:]).encode()
        return json.dumps(entries).encode()

    gcloud = FakeGcloud({"logging read": read})
    slept: list[float] = []
    source = lc.search_cloud_logging(
        gcloud, G.project_id, START, END, MARKER, NEEDLES, sleep=slept.append
    )
    assert source.passed
    assert polls["n"] == 3
    assert slept == [15, 15]
    assert source.report.items == 2
    full = [c for c in gcloud.calls if MARKER not in c[2]]
    assert full[0][2] == 'timestamp>="2026-11-20T10:00:00Z" AND timestamp<="2026-11-20T10:30:00Z"'
    assert f"--project={G.project_id}" in full[0]


def test_cloud_logging_fails_without_its_positive_control_and_on_a_canary() -> None:
    clock = iter(range(0, 10_000, 100))
    missing = lc.search_cloud_logging(
        FakeGcloud({"logging read": [_entry(1, "/v1/runs")]}),
        G.project_id,
        START,
        END,
        MARKER,
        NEEDLES,
        wait_seconds=250,
        sleep=lambda _: None,
        clock=lambda: float(next(clock)),
    )
    assert not missing.control_found
    assert "not ingested" in missing.report.errors[0]
    leaked = lc.search_cloud_logging(
        FakeGcloud({"logging read": [_entry(2, f"/{MARKER}"), _entry(3, f"AETERM {CANARY}")]}),
        G.project_id,
        START,
        END,
        MARKER,
        NEEDLES,
        sleep=lambda _: None,
    )
    assert leaked.control_found
    assert not leaked.passed
    assert leaked.canary_hits == [f"projects/{G.project_id}/logs/stdout id3"]


def test_the_verdict_needs_every_positive_control_and_no_canary() -> None:
    def source(name: str, canary: bool, control: bool) -> lc.Source:
        report = Report(name)
        if canary:
            report.hits[lc.CANARY] = ["somewhere"]
        return lc.Source(name, report, "c", control)

    check = lc.LeakCheck(env="gke", zones=["rbqm"], canary_sha256="0" * 64)
    assert not check.passed  # no source searched is no pass
    check.sources = [source("a", False, True), source("b", False, True)]
    assert check.passed
    check.sources.append(source("node n", False, False))
    assert not check.passed
    assert check.failures() == ["node n: positive control not found (c)"]
    check.sources[-1] = source("node n", True, True)
    assert check.failures() == ["node n: the canary was found in ['somewhere']"]
    check.sources.pop()
    check.layout = {"outside_tags": ["atespaces/meridian/actors/x/snapshots/y"]}
    assert not check.passed


def test_the_scan_pod_reads_the_node_read_only_and_holds_nothing_else() -> None:
    pod = lc.scan_pod_manifest(
        "scan-1", "gke-node-a", "us-central1-docker.pkg.dev/p/r/d@sha256:" + "e" * 64
    )
    spec = pod["spec"]
    container = spec["containers"][0]
    assert spec["nodeName"] == "gke-node-a"
    assert spec["automountServiceAccountToken"] is False
    assert spec["tolerations"] == [{"operator": "Exists"}]  # worker nodes are tainted
    assert spec["volumes"] == [{"name": "host", "hostPath": {"path": "/", "type": "Directory"}}]
    assert container["volumeMounts"] == [{"name": "host", "mountPath": "/host", "readOnly": True}]
    caps = container["securityContext"]["capabilities"]
    assert caps == {"drop": ["ALL"], "add": ["DAC_READ_SEARCH"]}
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert "privileged" not in container["securityContext"]
    assert CANARY not in json.dumps(pod)


def test_the_gke_overlay_points_substrate_at_the_cluster_by_digest() -> None:
    import tomllib
    from pathlib import Path

    raw = tomllib.loads(Path("deploy/gke/meridian.toml").read_text(encoding="utf-8"))
    s = SubstrateSettings.model_validate(raw["substrate"])
    assert s.target == "gke"
    assert (
        s.registry == s.gke.registry == "us-central1-docker.pkg.dev/meridian-fleet-agents/meridian"
    )
    assert s.context == f"gke_{s.gke.project_id}_{s.gke.zone}_{s.gke.cluster}"
    assert s.worker_image.startswith("ghcr.io/agent-substrate/substrate/ateworker-gvisor@sha256:")
    assert s.deploy_dir == Path("deploy/gke/substrate")
    assert s.node_selector == {"meridian.dev/pool": "substrate"}
    assert s.toleration["effect"] == "NoSchedule"
