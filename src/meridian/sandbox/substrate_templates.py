"""Agent Substrate ActorTemplates and the WorkerPool, rendered from the data below.

`meridian substrate render` writes deploy/substrate/; the files are committed and a test
checks that rendering reproduces them byte for byte (as for policies/).

* One ActorTemplate per policy without network rules that runs in substrate (rbqm,
  safety.sae_reconciler, quality, quality.audit_trail), named <policy>-<12 hex of the
  agent image's registry digest> because a template is immutable. Each runs
  `meridian-worker serve --areas <the policy's reads>` on port 8080 with a wakeup probe on
  /readyz, so the golden snapshot is taken after imports and warm-up and before any data.
  No EgressPolicy is ever created for these actors: Substrate's egress is default deny.
* One conformance template (conformance-<12 hex>) on the same image, pool, sandbox class
  and size, running `meridian-worker probe`: the only difference is the command
  (meridian.worker_probe), which answers fixed containment checks and never runs agents.
* One WorkerPool (meridian/meridian) of gVisor workers from the ateworker-gvisor image
  of the pinned release, sized for one actor per worker, placed only on nodes with the
  worker label and tolerating the worker taint (phase 4b step 3: the Spot pool on GKE).

Sizes: an actor takes its whole worker. Each worker pod is limited to 1 GiB (request =
limit, memory is not compressible) and an actor to 768 MiB of it; docs/substrate-eval.md
records what a run actually uses.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian.sandbox.policies import ALL_POLICIES, stage_path

SUBSTRATE_POLICIES: tuple[str, ...] = (
    "rbqm",
    "safety.sae_reconciler",
    "quality",
    "quality.audit_trail",
)
CONFORMANCE = "conformance"
WORKER_BIN = "/opt/meridian/venv/bin/meridian-worker"
PORT = 8080
WORKLOAD_LABEL = "meridian"
ACTOR_CPU_MILLICORES = 1000
ACTOR_MEMORY_MIB = 768  # default; [substrate] actor_memory_mib
MIB = 1024 * 1024
WORKER_CPU_LIMIT = "1"
WORKER_CPU_REQUEST = "250m"
WORKER_MEMORY_MIB = 1024  # default; [substrate] worker_memory_mib
# A worker offers its pod memory less this to actors (ateworker reports 960Mi of 1Gi).
WORKER_RESERVED_MIB = 64
TEMPLATES_DIR = "templates"
POOL_FILE = "workerpool.yaml"
ACCESS_POLICY_FILE = "access-policy.yaml"
# Fields ate-api adds or changes on its own; they are not part of what runs.
SERVER_METADATA = ("createTime", "updateTime", "version", "uid", "deleteTime")


class TemplateError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PoolSpec:
    namespace: str
    name: str
    replicas: int
    worker_image: str
    sandbox_class: str
    worker_memory_mib: int = WORKER_MEMORY_MIB
    node_selector: Mapping[str, str] | None = None
    toleration: Mapping[str, str] | None = None


def slug(policy: str) -> str:
    return policy.replace(".", "-").replace("_", "-")


def digest_tail(image_ref: str) -> str:
    """12 hex characters of the image's registry digest."""
    if "@sha256:" not in image_ref:
        raise TemplateError(f"{image_ref} is not pinned by digest")
    return image_ref.split("@sha256:", 1)[1][:12]


def template_name(policy: str, image_ref: str, actor_memory_mib: int = ACTOR_MEMORY_MIB) -> str:
    """<policy>-<12 hex of the image digest>-m<actor MiB>: templates are immutable, so a new
    image or a new actor size must give a new name."""
    return f"{slug(policy)}-{digest_tail(image_ref)}-m{actor_memory_mib}"


def check_sizes(actor_memory_mib: int, worker_memory_mib: int) -> None:
    if actor_memory_mib > worker_memory_mib - WORKER_RESERVED_MIB:
        raise TemplateError(
            f"an actor of {actor_memory_mib} MiB does not fit a {worker_memory_mib} MiB worker "
            f"(it offers {worker_memory_mib - WORKER_RESERVED_MIB} MiB)"
        )


def areas_of(policy: str) -> tuple[str, ...]:
    return ALL_POLICIES[policy].reads


def substrate_eligible(policy: str) -> bool:
    """Only a policy without network rules may run in substrate (default-deny egress)."""
    return not ALL_POLICIES[policy].network and not ALL_POLICIES[policy].providers


def _yaml_scalar(value: object) -> str:
    """Plain only for words of letters, digits and dashes that YAML cannot read as a
    number or a boolean; anything else is a JSON string (valid YAML)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    plain = (
        text.replace("-", "").isalnum()
        and not text.replace("-", "").isdigit()
        and text.lower() not in ("true", "false", "yes", "no", "null", "on", "off")
    )
    return text if plain else json.dumps(text)


def _yaml(value: object, indent: int = 0) -> list[str]:
    """A small block-style YAML writer for the nested dicts and lists below."""
    pad = " " * indent
    out: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, Mapping | list) and not item:
                out.append(f"{pad}{key}: {'{}' if isinstance(item, Mapping) else '[]'}")
            elif isinstance(item, Mapping | list):
                out.append(f"{pad}{key}:")
                out += _yaml(item, indent + 2)
            else:
                out.append(f"{pad}{key}: {_yaml_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                lines = _yaml(item, indent + 2)
                out.append(f"{pad}- {lines[0].lstrip()}")
                out += lines[1:]
            else:
                out.append(f"{pad}- {_yaml_scalar(item)}")
    return out


def template_spec(
    name: str,
    atespace: str,
    image_ref: str,
    command: Sequence[str],
    actor_memory_mib: int = ACTOR_MEMORY_MIB,
) -> dict[str, Any]:
    """The ActorTemplate, shaped like protojson ateapi.ActorTemplate."""
    return {
        "metadata": {"atespace": atespace, "name": name},
        "workerSelector": {"matchLabels": {"workload": WORKLOAD_LABEL}},
        "containers": [
            {
                "name": "worker",
                "image": image_ref,
                "command": list(command),
                "wakeupProbe": {"httpGet": {"path": "/readyz", "port": PORT}},
            }
        ],
        "resources": {
            "limits": {
                "cpuMillicores": ACTOR_CPU_MILLICORES,
                "memoryBytes": actor_memory_mib * MIB,
            }
        },
        "snapshotConfig": {"preferredFidelity": "SNAPSHOT_FIDELITY_MEMORY"},
    }


def zone_command(policy: str) -> list[str]:
    return [WORKER_BIN, "serve", "--port", str(PORT), "--areas", ",".join(areas_of(policy))]


def probe_command() -> list[str]:
    return [WORKER_BIN, "probe", "--port", str(PORT)]


def render_template(
    policy: str, atespace: str, image_ref: str, actor_memory_mib: int = ACTOR_MEMORY_MIB
) -> str:
    """policy is one of SUBSTRATE_POLICIES, or CONFORMANCE."""
    mem = actor_memory_mib
    if policy == CONFORMANCE:
        name = f"{CONFORMANCE}-{digest_tail(image_ref)}-m{mem}"
        spec = template_spec(name, atespace, image_ref, probe_command(), mem)
        what = "the conformance probe (meridian.worker_probe): fixed checks, no agents"
    else:
        if policy not in SUBSTRATE_POLICIES or not substrate_eligible(policy):
            raise TemplateError(f"{policy} may not run in substrate")
        name = template_name(policy, image_ref, mem)
        spec = template_spec(name, atespace, image_ref, zone_command(policy), mem)
        areas = ", ".join(stage_path(a) for a in areas_of(policy))
        what = f"policy {policy}: receives only {areas}"
    header = [
        f"# Agent Substrate ActorTemplate: {what}.",
        "# Generated by `meridian substrate render` from",
        "# src/meridian/sandbox/substrate_templates.py; do not edit by hand.",
    ]
    return "\n".join([*header, *_yaml(spec)]) + "\n"


def render_pool(pool: PoolSpec) -> str:
    memory = f"{pool.worker_memory_mib}Mi"
    spec = {
        "apiVersion": "ate.dev/v1",
        "kind": "WorkerPool",
        "metadata": {
            "name": pool.name,
            "namespace": pool.namespace,
            "labels": {"workload": WORKLOAD_LABEL},
        },
        "spec": {
            "replicas": pool.replicas,
            "workerImage": pool.worker_image,
            "sandboxClasses": [{"name": pool.sandbox_class}],
            "template": {
                **({"nodeSelector": dict(pool.node_selector)} if pool.node_selector else {}),
                **({"tolerations": [dict(pool.toleration)]} if pool.toleration else {}),
                "resources": {
                    "limits": {"cpu": WORKER_CPU_LIMIT, "memory": memory},
                    "requests": {"cpu": WORKER_CPU_REQUEST, "memory": memory},
                },
            },
        },
    }
    header = [
        "# Agent Substrate WorkerPool for meridian's actors (one actor per worker).",
        "# Generated by `meridian substrate render` from",
        "# src/meridian/sandbox/substrate_templates.py; do not edit by hand.",
        "apiVersion: v1",
        "kind: Namespace",
        "metadata:",
        f"  name: {pool.namespace}",
        "---",
    ]
    return "\n".join([*header, *_yaml(spec)]) + "\n"


def access_policy_spec(principals: Sequence[str]) -> dict[str, Any]:
    """The atespace's access policy: each principal is `editor` there and nowhere else.
    Since phase 4b step 3 the only principal is the in-cluster dispatcher (the laptop's
    runner identity is gone). v1.0.0 has no finer role: an editor may also suspend,
    revert and tag; the dispatcher's client refuses those (substrate.py DISPATCHER_VERBS)."""
    return {
        "metadata": {"name": "default"},
        "bindings": [{"role": "editor", "members": list(principals)}],
    }


def render_access_policy(atespace: str, principals: Sequence[str]) -> str:
    header = [
        f"# The access policy of atespace {atespace} (kubectl ate create access-policy -a",
        f"# {atespace}). Generated by `meridian substrate render`; do not edit by hand.",
    ]
    return "\n".join([*header, *_yaml(access_policy_spec(principals))]) + "\n"


def template_path(deploy_dir: Path, policy: str) -> Path:
    return deploy_dir / TEMPLATES_DIR / f"{policy}.yaml"


def committed_image(path: Path) -> str:
    """The image reference a committed template file names."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("image:"):
            return stripped.split(":", 1)[1].strip().strip('"')
    raise TemplateError(f"{path} names no image")


def committed_name(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("name:") and "worker" not in stripped:
            return stripped.split(":", 1)[1].strip()
    raise TemplateError(f"{path} names no template")


def canonical_spec(server: Mapping[str, Any]) -> dict[str, Any]:
    """The template as ate-api reports it, without status and server bookkeeping."""
    spec = {k: v for k, v in server.items() if k != "status"}
    metadata = {k: v for k, v in dict(spec.get("metadata", {})).items() if k not in SERVER_METADATA}
    return {**spec, "metadata": metadata}


def spec_sha256(server: Mapping[str, Any]) -> str:
    text = json.dumps(canonical_spec(server), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def golden_tag_of(server: Mapping[str, Any]) -> str | None:
    status = dict(server.get("status", {})).get("goldenSnapshotStatus", {})
    tag = dict(status).get("goldenTag")
    return None if not tag else str(dict(tag)["name"])


def golden_error_of(server: Mapping[str, Any]) -> str | None:
    status = dict(server.get("status", {})).get("goldenSnapshotStatus", {})
    message = dict(status).get("errorMessage")
    return str(message) if message else None
