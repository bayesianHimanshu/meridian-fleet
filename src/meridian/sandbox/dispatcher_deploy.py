"""Deploying the dispatcher (phase 4b step 2): its manifests, its token, its identity.

`meridian dispatcher deploy` builds the image (docker/dispatcher.Dockerfile, with the
kubectl-ate binary from go.ate.dev/substrate v1.0.0), pushes it, renders
deploy/dispatcher/dispatcher.yaml pinned by digest, applies it, puts the dispatcher's
ServiceAccount into the atespace's access policy (editor, the atespace only) and, on kind,
adds the dispatcher's SPIFFE ID to atenet-router's ingress allow-list. On GKE that list is
set when Substrate is installed (docs/gke-plan.md 6.7).

What the manifests give the dispatcher, and nothing more:
- namespace `meridian-dispatch`, labelled `ate.dev/api-access=true` (ate-api-server's
  NetworkPolicy admits non-worker pods of such namespaces) and with a default-deny
  ingress NetworkPolicy: the laptop's port-forward reaches the pod through the kubelet,
  nothing else may;
- ServiceAccount `dispatcher`: Kubernetes RBAC to read the WorkerPool (capacity) in the
  pool's namespace and ClusterTrustBundles (kubectl-ate verifies ate-api-server's
  certificate with them); Substrate `editor` on the atespace;
- a projected ServiceAccount token with the ate-api audience, a podidentity client
  certificate for atenet-router, and the shared token from a Secret;
- a read-only root filesystem, no capabilities, non-root, and an in-memory /tmp.
"""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from meridian.platform.config import SubstrateSettings
from meridian.sandbox.substrate_templates import _yaml

DOCKERFILE = Path("docker/dispatcher.Dockerfile")
IMAGE_NAME = "meridian-dispatcher"
MANIFEST = "dispatcher.yaml"
SECRET = "meridian-dispatcher-token"  # noqa: S105 (a Secret name, not a value)
ATE_TOKEN_PATH = "/run/ate/token"  # noqa: S105 (a path)
ROUTER_DIR = "/run/router-client"
TOKEN_DIR = "/run/dispatcher"  # noqa: S105 (a path)
WORKLOAD = "meridian-dispatcher"


class DispatcherDeployError(Exception):
    pass


def spiffe_id(s: SubstrateSettings) -> str:
    return f"spiffe://cluster.local/ns/{s.dispatcher_namespace}/sa/{s.dispatcher_service_account}"


def principal(s: SubstrateSettings) -> str:
    return f"user:system:serviceaccount:{s.dispatcher_namespace}:{s.dispatcher_service_account}"


def manifests(s: SubstrateSettings, image: str, kubectl_ate_sha256: str) -> list[dict[str, Any]]:
    ns, sa, name = s.dispatcher_namespace, s.dispatcher_service_account, s.dispatcher_deployment
    labels = {"workload": WORKLOAD}
    container = {
        "name": "dispatcher",
        "image": image,
        "args": [
            "serve",
            "--port",
            str(s.dispatcher_port),
            "--token-file",
            f"{TOKEN_DIR}/token",
            "--atespace",
            s.atespace,
            "--api-endpoint",
            s.api_endpoint,
            "--ate-token-file",
            ATE_TOKEN_PATH,
            "--router",
            s.router_address,
            "--router-cert",
            f"{ROUTER_DIR}/credential-bundle.pem",
            "--router-ca",
            f"{ROUTER_DIR}/router-ca.pem",
            "--pool",
            f"{s.namespace}/{s.worker_pool}",
            "--actor-port",
            str(s.port),
            "--placement-wait",
            str(s.placement_wait_seconds),
            "--request-timeout",
            str(s.request_timeout_seconds),
        ],
        "ports": [{"name": "http", "containerPort": s.dispatcher_port}],
        "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 5},
        "resources": {
            "requests": {"cpu": "250m", "memory": "256Mi"},
            "limits": {"cpu": "2", "memory": "1Gi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumeMounts": [
            {"name": "token", "mountPath": TOKEN_DIR, "readOnly": True},
            {"name": "ate-token", "mountPath": "/run/ate", "readOnly": True},
            {"name": "router-client", "mountPath": ROUTER_DIR, "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},  # noqa: S108 (in-memory, the pod's own)
        ],
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": labels,
            "annotations": {"meridian.dev/kubectl-ate-sha256": kubectl_ate_sha256},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": sa,
                    "enableServiceLinks": False,
                    # The mounted token, certificate and ServiceAccount token are group
                    # readable by gid 1000 only (fsGroup), never world readable.
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "fsGroup": 1000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [container],
                    "volumes": [
                        {"name": "token", "secret": {"secretName": SECRET, "defaultMode": 0o440}},
                        {
                            "name": "ate-token",
                            "projected": {
                                "sources": [
                                    {
                                        "serviceAccountToken": {
                                            "audience": s.api_audience,
                                            "expirationSeconds": 3600,
                                            "path": "token",
                                        }
                                    }
                                ]
                            },
                        },
                        {
                            "name": "router-client",
                            "projected": {
                                "sources": [
                                    {
                                        "podCertificate": {
                                            "signerName": "podidentity.podcert.ate.dev/identity",
                                            "keyType": "ECDSAP256",
                                            "credentialBundlePath": "credential-bundle.pem",
                                        }
                                    },
                                    {
                                        "clusterTrustBundle": {
                                            "signerName": "servicedns.podcert.ate.dev/identity",
                                            "labelSelector": {
                                                "matchLabels": {"podcert.ate.dev/canarying": "live"}
                                            },
                                            "path": "router-ca.pem",
                                        }
                                    },
                                ]
                            },
                        },
                        {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}},
                    ],
                },
            },
        },
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": ns, "labels": {**labels, "ate.dev/api-access": "true"}},
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": sa, "namespace": ns, "labels": labels},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "default-deny-ingress", "namespace": ns, "labels": labels},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"]},
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {
                "name": "meridian-dispatcher-pool",
                "namespace": s.namespace,
                "labels": labels,
            },
            "rules": [
                {
                    "apiGroups": ["ate.dev"],
                    "resources": ["workerpools"],
                    "resourceNames": [s.worker_pool],
                    "verbs": ["get"],
                }
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": "meridian-dispatcher-pool",
                "namespace": s.namespace,
                "labels": labels,
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "meridian-dispatcher-pool",
            },
            "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": ns}],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "meridian-dispatcher-trust", "labels": labels},
            "rules": [
                {
                    "apiGroups": ["certificates.k8s.io"],
                    "resources": ["clustertrustbundles"],
                    "verbs": ["get", "list", "watch"],
                }
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "meridian-dispatcher-trust", "labels": labels},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "meridian-dispatcher-trust",
            },
            "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": ns}],
        },
        deployment,
    ]


def render(s: SubstrateSettings, image: str, kubectl_ate_sha256: str) -> str:
    header = [
        "# The Meridian dispatcher (phase 4b step 2): namespace, identity, RBAC and Deployment.",
        "# Generated by `meridian dispatcher render` from",
        "# src/meridian/sandbox/dispatcher_deploy.py; do not edit by hand. The token Secret",
        f"# ({SECRET}) is created by `meridian dispatcher token`, never committed.",
    ]
    docs = ["\n".join(_yaml(m)) for m in manifests(s, image, kubectl_ate_sha256)]
    return "\n".join(header) + "\n" + "\n---\n".join(docs) + "\n"


def committed_image(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("image:"):
            return stripped.split(":", 1)[1].strip().strip('"')
    raise DispatcherDeployError(f"{path} names no image")


def _run(argv: Sequence[str], *, input_text: str | None = None, timeout: float = 600) -> str:
    try:
        done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
            list(argv),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DispatcherDeployError(f"{argv[0]} failed: {exc}") from exc
    if done.returncode != 0:
        raise DispatcherDeployError(f"{' '.join(argv[:5])}: {done.stderr[-800:]}")
    return done.stdout


def apply_token_secret(kubectl: str, context: str, s: SubstrateSettings, token_file: Path) -> None:
    """Create or update the Secret from the laptop's token file. The token goes through
    kubectl's stdin as a manifest, never on a command line or to the terminal."""
    import base64

    token = token_file.expanduser().read_bytes().strip()
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": SECRET,
            "namespace": s.dispatcher_namespace,
            "labels": {"workload": WORKLOAD},
        },
        "type": "Opaque",
        "data": {"token": base64.b64encode(token).decode()},
    }
    _run([kubectl, "--context", context, "apply", "-f", "-"], input_text=json.dumps(manifest))


def allow_router_ingress(kubectl: str, context: str, s: SubstrateSettings) -> str:
    """kind only: add the dispatcher's SPIFFE ID to atenet-router's ingress allow-list,
    keeping the IDs already there. (On GKE ate-setup sets the list at install.) A later
    `ate-setup deploy ate-system` resets it to whatever that command is given."""
    base = [kubectl, "--context", context, "-n", "ate-system"]
    deployment = json.loads(_run([*base, "get", "deployment", "atenet-router", "-o", "json"]))
    containers = deployment["spec"]["template"]["spec"]["containers"]
    index = next(i for i, c in enumerate(containers) if c["name"] == "atenet-router")
    args = list(containers[index].get("args", []))
    flag = "--ingress-allowed-spiffe-ids="
    position = next((i for i, a in enumerate(args) if a.startswith(flag)), None)
    current = [] if position is None else args[position].removeprefix(flag).split(",")
    wanted = spiffe_id(s)
    if wanted in current:
        return "unchanged"
    value = flag + ",".join([*current, wanted])
    if position is None:
        patch = [
            {"op": "add", "path": f"/spec/template/spec/containers/{index}/args/-", "value": value}
        ]
    else:
        path = f"/spec/template/spec/containers/{index}/args/{position}"
        patch = [{"op": "replace", "path": path, "value": value}]
    _run([*base, "patch", "deployment", "atenet-router", "--type", "json", "-p", json.dumps(patch)])
    _run([*base, "rollout", "status", "deployment/atenet-router", "--timeout=180s"], timeout=210)
    return "added"
