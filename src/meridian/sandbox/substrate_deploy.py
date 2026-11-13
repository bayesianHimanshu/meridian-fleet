"""`meridian substrate deploy`: what this project sets up in its own atespace.

Only these actions, all on objects this code names: push the agent image to the kind
registry (127.0.0.1:5001; on GKE you push it to Artifact Registry and this code only
reads its digest, resolve_pushed), apply the meridian Namespace and WorkerPool, create the
meridian atespace, create the ActorTemplates rendered in deploy/substrate/ and wait for
their golden snapshots. It never changes ate-system, other namespaces or global settings.
Templates are immutable: a new image gives new template names (the digest is part of
the name); `prune_templates` deletes this atespace's templates that the committed files
no longer name.
"""

import json
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.sandbox.image import DIRTY_LABEL, REVISION_LABEL, ImageInfo
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


def _registry_get(url: str, accept: str | None = None) -> bytes:
    request = urllib.request.Request(  # noqa: S310 (the local kind registry)
        url, headers={"Accept": accept} if accept else {}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return bytes(response.read())


def registry_config_digest(image_ref: str, registry_url: str = "http://127.0.0.1:5001") -> str:
    """The config digest (the image id) of a pushed image, read from the registry."""
    repo_part, digest = image_ref.split("@", 1)
    repo = repo_part.split("/", 1)[1]
    raw = _registry_get(f"{registry_url}/v2/{repo}/manifests/{digest}", MANIFEST_TYPES)
    return str(json.loads(raw)["config"]["digest"])


def registry_image_labels(
    image_ref: str, registry_url: str = "http://127.0.0.1:5001"
) -> dict[str, str]:
    """The labels in a pushed image's config, read from the registry (what actors run)."""
    repo_part, _ = image_ref.split("@", 1)
    repo = repo_part.split("/", 1)[1]
    config = registry_config_digest(image_ref, registry_url)
    blob = json.loads(_registry_get(f"{registry_url}/v2/{repo}/blobs/{config}"))
    return {str(k): str(v) for k, v in dict(blob.get("config", {}).get("Labels") or {}).items()}


def require_clean_source(info: ImageInfo, repo: Path = Path()) -> str:
    """The commit an image was built from; refuse an image without the label, built from
    a dirty tree, or naming a commit this repository does not have."""
    if not info.revision or info.dirty is None:
        raise DeployError(
            f"{info.ref} has no {REVISION_LABEL}/{DIRTY_LABEL} labels; rebuild it with "
            "`meridian image build`"
        )
    if info.dirty:
        raise DeployError(
            f"{info.ref} was built from a dirty tree at {info.revision[:12]}; commit the "
            "image sources and rebuild before deploying templates"
        )
    done = subprocess.run(  # noqa: S603 (fixed argv)
        ["git", "cat-file", "-e", f"{info.revision}^{{commit}}"],  # noqa: S607
        capture_output=True,
        check=False,
        cwd=repo,
    )
    if done.returncode != 0:
        raise DeployError(f"{info.ref} names commit {info.revision}, which is not in this repo")
    return info.revision


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


def _inspect(ref: str) -> dict[str, object]:
    raw = json.loads(_run(["docker", "image", "inspect", ref, "--format", "{{json .}}"]))
    assert isinstance(raw, dict)
    return raw


def local_digest_ref(local_ref: str, registry: str) -> str:
    """<registry>/<name>@sha256:<manifest digest> as docker recorded it when the image was
    pushed to registry (RepoDigests); refuse if it was never pushed there."""
    name = local_ref.split("/")[-1].rsplit(":", 1)[0]
    repo = f"{registry}/{name}"
    digests = [str(d) for d in list(_inspect(local_ref).get("RepoDigests") or [])]  # type: ignore[call-overload]
    pinned = [d for d in digests if d.startswith(f"{repo}@sha256:")]
    if not pinned:
        raise DeployError(f"{local_ref} was never pushed to {registry} (no RepoDigest for {repo})")
    return pinned[0]


def ar_digest(gcloud: str, tagged_ref: str) -> str:
    """The manifest digest Artifact Registry holds for a tag (read-only gcloud)."""
    out = _run(
        [
            gcloud,
            "artifacts",
            "docker",
            "images",
            "describe",
            tagged_ref,
            "--format=value(image_summary.digest)",
        ],
        timeout=120,
    ).strip()
    if not out.startswith("sha256:") or len(out) != 71:
        raise DeployError(f"Artifact Registry reports no digest for {tagged_ref}: {out!r}")
    return out


def resolve_pushed(local_ref: str, registry: str, gcloud: str) -> PushedImage:
    """GKE: the image you pushed to Artifact Registry (docs/gke-session.md), by digest. The
    digest docker recorded at the push must be the one Artifact Registry holds for the
    tag, and the local image (whose id is the config digest) must be the one pushed."""
    ref = local_digest_ref(local_ref, registry)
    tag = local_ref.rsplit(":", 1)[1] if ":" in local_ref.split("/")[-1] else "latest"
    tagged = f"{ref.split('@', 1)[0]}:{tag}"
    held = ar_digest(gcloud, tagged)
    if held != ref.split("@", 1)[1]:
        raise DeployError(
            f"Artifact Registry holds {held} for {tagged}, but {local_ref} was pushed as {ref}; "
            "push it again"
        )
    return PushedImage(ref=ref, config_digest=str(_inspect(local_ref)["Id"]))


def image_config_digest(image_ref: str, target: str) -> str:
    """The config digest (image id) of an image named by digest: from the kind registry,
    or, for Artifact Registry, from the local copy that was pushed (resolve_pushed)."""
    if target == "kind":
        return registry_config_digest(image_ref)
    return str(_inspect(image_ref)["Id"])


def image_labels(image_ref: str, target: str) -> dict[str, str]:
    if target == "kind":
        return registry_image_labels(image_ref)
    config = dict(_inspect(image_ref).get("Config") or {})  # type: ignore[call-overload]
    return {str(k): str(v) for k, v in dict(config.get("Labels") or {}).items()}


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


def ensure_access_policy(
    kubectl: str,
    context: str,
    atespace: str,
    policy_file: Path,
    expected_bindings: list[dict[str, object]],
) -> str:
    """Make the atespace's access policy the committed one (create it, or update it with
    the server's uid and version)."""
    ate = [kubectl, "ate", "--context", context]
    try:
        current = json.loads(_run([*ate, "get", "access-policy", "-a", atespace, "-o", "json"]))
    except DeployError as exc:
        if "no access policy" not in str(exc) and "NotFound" not in str(exc):
            raise
        _run([*ate, "create", "access-policy", "-a", atespace, "-f", str(policy_file)])
        return "created"
    if current.get("bindings") == expected_bindings:
        return "unchanged"
    meta = current.get("metadata", {})
    manifest = {
        "metadata": {"name": "default", "uid": meta.get("uid"), "version": meta.get("version")},
        "bindings": expected_bindings,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(manifest, handle)
    try:
        _run([*ate, "update", "access-policy", "-a", atespace, "-f", handle.name])
    finally:
        Path(handle.name).unlink(missing_ok=True)
    return "updated"


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
