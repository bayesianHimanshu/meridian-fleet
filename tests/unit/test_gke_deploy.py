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
    assert '      - key: "ate.dev/sandboxClass"\n        operator: Equal\n' in text
    assert "        value: gvisor\n        effect: NoSchedule\n" in text
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


def test_a_worker_node_without_a_ready_atelet_fails_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 on GKE: a taint atelet did not tolerate kept it off the Spot node, and the
    workers there waited for its socket, Running but never Ready."""
    from meridian.sandbox import placement

    s = SubstrateSettings()
    key, value = next(iter(s.node_selector.items()))
    t = s.toleration
    taint = {"key": t["key"], "value": t["value"], "effect": t["effect"]}
    nodes = {
        "items": [
            {"metadata": {"name": "sys", "labels": {}}, "spec": {}},
            {"metadata": {"name": "spot", "labels": {key: value}}, "spec": {"taints": [taint]}},
        ]
    }
    ready = [{"type": "Ready", "status": "True"}]
    unready = [{"type": "Ready", "status": "False"}]

    def answers(atelet_on: str) -> dict[str, dict[str, Any]]:
        pool = {"spec": {"template": {"nodeSelector": s.node_selector, "tolerations": [t]}}}
        workers = [
            {
                "metadata": {"name": f"w{i}"},
                "spec": {"nodeName": "spot"},
                "status": {
                    "phase": "Running",
                    "conditions": ready if atelet_on == "spot" else unready,
                },
            }
            for i in range(2)
        ]
        atelet = {"spec": {"nodeName": atelet_on}, "status": {"conditions": ready}}
        dispatcher = {"spec": {"nodeName": "sys"}}
        return {
            "workerpool": pool,
            "nodes": nodes,
            "app=atelet": {"items": [atelet]},
            f"ate.dev/worker-pool={s.worker_pool}": {"items": workers},
            "app=d": {"items": [dispatcher]},
        }

    for atelet_on, passed in (("sys", False), ("spot", True)):
        table = answers(atelet_on)

        def fake_json(_: SubstrateSettings, *args: str, table: Any = table) -> Any:
            return next(v for k, v in table.items() if k in args)

        monkeypatch.setattr(placement, "_json", fake_json)
        monkeypatch.setattr(placement, "untolerated_probe", lambda _: (True, "refused"))
        checks = {n: ok for n, ok, _ in placement.check(s, False, "app=d").checks}
        assert checks["atelet_on_labelled_nodes"] is passed
        assert checks["workers_on_labelled_nodes"] is passed
        assert checks["labelled_nodes_tainted"]


def test_a_new_atespace_gets_the_committed_access_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """kubectl-ate 1.0.0 answers a new atespace with exit 0, no stdout and "has no access
    policy" on stderr (seen on GKE, step 4): that is a create, not a parse error."""
    policy = tmp_path / "access-policy.yaml"
    policy.write_text("bindings: []\n", encoding="utf-8")
    run = _Run(
        {"kubectl ate --context gke get access-policy": "", "kubectl ate --context gke create": ""}
    )
    monkeypatch.setattr(subprocess, "run", run)
    assert d.ensure_access_policy("kubectl", "gke", "meridian", policy, []) == "created"
    assert run.calls[-1][4:6] == ["create", "access-policy"]


def test_the_dispatcher_starts_with_core_dumps_off() -> None:
    manifests = dd.manifests(SubstrateSettings(), "r@" + DIGEST, "0" * 64)
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["/usr/bin/sh", "-c", dd.NO_CORE, dd.ENTRYPOINT]
    assert dd.NO_CORE.startswith("ulimit -c 0 && exec ")
    assert container["args"][0] == "serve"
