"""Deploy code for GKE (phase 4b step 3, item A), without a cluster or a cloud: images by
digest from Artifact Registry, the WorkerPool's placement, the router admitting only the
dispatcher, and the settings overlay."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from meridian.platform.config import CONFIG_OVERLAY_ENV, Settings, SubstrateSettings
from meridian.sandbox import dispatcher_deploy as dd
from meridian.sandbox import substrate_deploy as d
from meridian.sandbox.substrate_templates import PoolSpec, render_pool

AR = "us-central1-docker.pkg.dev/meridian-fleet-agents/meridian"
DIGEST = "sha256:" + "a" * 64


class _Run:
    """subprocess.run replaced: answers by the first words of the argv, records them."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        line = " ".join(argv)
        for prefix, out in self.answers.items():
            if line.startswith(prefix):
                return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 1, "", f"unexpected: {line}")


def _inspect(repo_digests: list[str]) -> str:
    return json.dumps({"Id": "sha256:" + "c" * 64, "RepoDigests": repo_digests})


def test_the_gke_image_is_the_one_artifact_registry_holds_for_the_pushed_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _Run(
        {
            "docker image inspect meridian-agent:0.1.0": _inspect(
                [
                    f"localhost:5001/meridian-agent@sha256:{'f' * 64}",
                    f"{AR}/meridian-agent@{DIGEST}",
                ]
            ),
            "gcloud artifacts docker images describe": DIGEST + "\n",
        }
    )
    monkeypatch.setattr(subprocess, "run", run)
    pushed = d.resolve_pushed("meridian-agent:0.1.0", AR, "gcloud")
    assert pushed.ref == f"{AR}/meridian-agent@{DIGEST}"
    assert pushed.config_digest == "sha256:" + "c" * 64
    describe = next(c for c in run.calls if c[0] == "gcloud")
    assert describe[1:5] == ["artifacts", "docker", "images", "describe"]
    assert describe[5] == f"{AR}/meridian-agent:0.1.0"
    assert not any(c[:2] == ["docker", "push"] for c in run.calls)  # nothing is pushed


def test_a_stale_or_missing_push_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    other = "sha256:" + "b" * 64
    monkeypatch.setattr(
        subprocess,
        "run",
        _Run(
            {
                "docker image inspect": _inspect([f"{AR}/meridian-agent@{DIGEST}"]),
                "gcloud artifacts": other + "\n",
            }
        ),
    )
    with pytest.raises(d.DeployError, match="push it again"):
        d.resolve_pushed("meridian-agent:0.1.0", AR, "gcloud")
    monkeypatch.setattr(subprocess, "run", _Run({"docker image inspect": _inspect([])}))
    with pytest.raises(d.DeployError, match="never pushed"):
        d.resolve_pushed("meridian-agent:0.1.0", AR, "gcloud")


def test_the_worker_pool_goes_only_to_labelled_nodes_and_tolerates_their_taint() -> None:
    s = SubstrateSettings()
    pool = PoolSpec(
        namespace="meridian",
        name="meridian",
        replicas=2,
        worker_image=s.worker_image,
        sandbox_class="gvisor",
        node_selector=s.node_selector,
        toleration=s.toleration,
    )
    text = render_pool(pool)
    assert "    nodeSelector:\n      meridian.dev/pool: substrate\n" in text
    assert '      - key: "meridian.dev/worker"\n        operator: Equal\n' in text
    assert '        value: "true"\n        effect: NoSchedule\n' in text
    committed = Path("deploy/substrate/workerpool.yaml").read_text(encoding="utf-8")
    assert "meridian.dev/pool: substrate" in committed


def _router_json(args: list[str]) -> str:
    container = {"name": "atenet-router", "args": ["--port=8444", *args]}
    return json.dumps({"spec": {"template": {"spec": {"containers": [container]}}}})


def test_kind_router_is_set_to_the_dispatcher_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    s = SubstrateSettings()
    client = "spiffe://cluster.local/ns/ate-system/sa/atenet-client"
    run = _Run(
        {
            "kubectl --context kind-kind -n ate-system get deployment atenet-router": _router_json(
                [f"{dd.ROUTER_FLAG}{client},{dd.spiffe_id(s)}"]
            ),
            "kubectl --context kind-kind -n ate-system patch": "",
            "kubectl --context kind-kind -n ate-system rollout": "",
        }
    )
    monkeypatch.setattr(subprocess, "run", run)
    assert "was " + client in dd.set_router_ingress("kubectl", "kind-kind", s)
    patch = json.loads(next(c for c in run.calls if "patch" in c)[-1])
    assert patch == [
        {
            "op": "replace",
            "path": "/spec/template/spec/containers/0/args/1",
            "value": dd.ROUTER_FLAG + dd.spiffe_id(s),
        }
    ]


def test_gke_router_must_admit_only_the_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    s = SubstrateSettings()
    good = _Run({"kubectl": _router_json([dd.ROUTER_FLAG + dd.spiffe_id(s)])})
    monkeypatch.setattr(subprocess, "run", good)
    assert "only" in dd.require_router_only_dispatcher("kubectl", "gke", s)
    assert not any("patch" in c for c in good.calls)  # GKE: checked, never patched
    for args in ([], [dd.ROUTER_FLAG + dd.spiffe_id(s) + ",spiffe://x/ns/a/sa/b"]):
        monkeypatch.setattr(subprocess, "run", _Run({"kubectl": _router_json(args)}))
        with pytest.raises(dd.DispatcherDeployError, match="not only"):
            dd.require_router_only_dispatcher("kubectl", "gke", s)


def test_the_dispatcher_knows_its_own_address() -> None:
    manifests = dd.manifests(SubstrateSettings(), "r@" + DIGEST, "0" * 64)
    deployment = manifests[-1]
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env == [{"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}]


def test_the_overlay_merges_over_the_default_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_OVERLAY_ENV, "deploy/gke/meridian.toml")
    s = Settings()
    assert s.substrate.target == "gke"
    assert s.substrate.registry == AR
    # Tables merge key by key: what the overlay leaves out comes from config/default.toml.
    assert s.substrate.worker_memory_mib == 512
    assert s.substrate.atespace == "meridian"
    assert s.runtime.executors["rbqm"] == "substrate"
    monkeypatch.setenv(CONFIG_OVERLAY_ENV, "deploy/gke/missing.toml")
    with pytest.raises(FileNotFoundError):
        Settings()
