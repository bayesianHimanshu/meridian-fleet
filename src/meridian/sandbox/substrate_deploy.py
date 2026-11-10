"""`meridian substrate deploy`: what this project sets up in its own atespace.

Only these actions, all on objects this code names: push the agent image to the kind
registry (127.0.0.1:5001), apply the meridian Namespace and WorkerPool, create the
meridian atespace, create the ActorTemplates rendered in deploy/substrate/ and wait for
their golden snapshots. It never changes ate-system, other namespaces or global settings.
Templates are immutable: a new image gives new template names (the digest is part of
the name); `prune_templates` deletes this atespace's templates that the committed files
no longer name.
"""

import json
import subprocess
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.sandbox.substrate import KubectlAteClient, NotFoundError, SubstrateError
from meridian.sandbox.substrate_templates import (
    golden_error_of,
    golden_tag_of,
)

MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class DeployError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PushedImage:
    ref: str  # <registry>/<name>@sha256:<manifest digest>
    config_digest: str  # sha256:<image id>, what `docker image inspect` reports as Id


def _run(argv: Sequence[str], timeout: float = 600) -> str:
    try:
        done = subprocess.run(  # noqa: S603 (argv built from fixed parts)
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"{argv[0]} {argv[1]} failed: {exc}") from exc
    if done.returncode != 0:
        raise DeployError(f"{' '.join(argv[:3])}: {done.stderr.strip()[-800:]}")
    return done.stdout


def registry_config_digest(image_ref: str, registry_url: str = "http://127.0.0.1:5001") -> str:
    """The config digest (the image id) of a pushed image, read from the registry."""
    repo_part, digest = image_ref.split("@", 1)
    repo = repo_part.split("/", 1)[1]
    request = urllib.request.Request(  # noqa: S310 (the local kind registry)
        f"{registry_url}/v2/{repo}/manifests/{digest}", headers={"Accept": MANIFEST_TYPES}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        manifest = json.loads(response.read())
    return str(manifest["config"]["digest"])


def push_image(local_ref: str, registry: str) -> PushedImage:
    """docker tag and push to the kind registry; the reference returned is by digest."""
    name = local_ref.split("/")[-1]
    remote = f"{registry}/{name}"
    _run(["docker", "tag", local_ref, remote])
    _run(["docker", "push", remote])
    repo_digests = json.loads(
        _run(["docker", "image", "inspect", remote, "--format", "{{json .RepoDigests}}"])
    )
    repo = remote.rsplit(":", 1)[0]
    pinned = [d for d in repo_digests if d.startswith(f"{repo}@sha256:")]
    if not pinned:
        raise DeployError(f"docker reports no digest for {remote}")
    ref = pinned[0]
    image_id = _run(["docker", "image", "inspect", local_ref, "--format", "{{.Id}}"]).strip()
    config = registry_config_digest(ref)
    if config != image_id:
        raise DeployError(f"the registry holds config {config} for {ref}, not {image_id}")
    return PushedImage(ref=ref, config_digest=config)


def apply_pool(kubectl: str, context: str, pool_file: Path) -> None:
    _run([kubectl, "--context", context, "apply", "-f", str(pool_file)])


def wait_pool(kubectl: str, context: str, namespace: str, name: str, timeout: float) -> None:
    _run(
        [
            kubectl,
            "--context",
            context,
            "-n",
            namespace,
            "rollout",
            "status",
            f"deployment/{name}",
            f"--timeout={int(timeout)}s",
        ],
        timeout=timeout + 30,
    )


def ensure_atespace(client: KubectlAteClient, kubectl: str, context: str) -> None:
    argv = [kubectl, "ate", "--context", context, "get", "atespace", client.atespace]
    try:
        _run(argv)
    except DeployError:
        _run([kubectl, "ate", "--context", context, "create", "atespace", client.atespace])


def create_template(client: KubectlAteClient, kubectl: str, context: str, path: Path) -> str:
    """Create the template in path unless it exists (same name means same digest and
    spec: rendering is deterministic and the name carries the image digest)."""
    from meridian.sandbox.substrate_templates import committed_name

    name = committed_name(path)
    try:
        client.get_template(name)
        return name
    except NotFoundError:
        pass
    _run([kubectl, "ate", "--context", context, "create", "actor-template", "-f", str(path)])
    return name


def wait_golden(client: KubectlAteClient, names: Sequence[str], timeout: float) -> dict[str, float]:
    """Seconds until each template's golden tag appeared (as observed by polling)."""
    start = time.monotonic()
    waiting = set(names)
    ready: dict[str, float] = {}
    while waiting:
        for name in sorted(waiting):
            server = client.get_template(name)
            error = golden_error_of(server)
            if error:
                raise DeployError(f"golden snapshot of {name} failed: {error}")
            if golden_tag_of(server):
                ready[name] = round(time.monotonic() - start, 1)
                waiting.discard(name)
        if waiting and time.monotonic() - start > timeout:
            raise DeployError(f"no golden snapshot after {timeout:.0f} s: {sorted(waiting)}")
        if waiting:
            time.sleep(1.0)
    return ready


def prune_templates(
    client: KubectlAteClient, kubectl: str, context: str, keep: Sequence[str]
) -> list[str]:
    listed = json.loads(
        _run(
            [
                kubectl,
                "ate",
                "--context",
                context,
                "get",
                "actor-template",
                "-a",
                client.atespace,
                "-o",
                "json",
            ]
        )
        or "{}"
    )
    names = [str(t["metadata"]["name"]) for t in listed.get("actorTemplates", [])]
    removed = []
    for name in names:
        if name in keep:
            continue
        try:
            _run(
                [
                    kubectl,
                    "ate",
                    "--context",
                    context,
                    "delete",
                    "actor-template",
                    name,
                    "-a",
                    client.atespace,
                ]
            )
        except DeployError as exc:
            raise SubstrateError(str(exc)) from exc
        removed.append(name)
    return removed
