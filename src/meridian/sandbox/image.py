"""The agent image: build it with docker and read back its digest and size."""

import json
import subprocess
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

DOCKERFILE = Path("docker/agent.Dockerfile")
IMAGE_NAME = "meridian-agent"
REVISION_LABEL = "org.opencontainers.image.revision"
DIRTY_LABEL = "dev.meridian.source.dirty"
# What enters the image (docker/agent.Dockerfile and .dockerignore), and the Dockerfile.
IMAGE_SOURCES = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
    "config/default.toml",
    "config/sites.toml",
    "config/safety_terms.toml",
    "config/tmf_expected.toml",
    "docker/agent.Dockerfile",
    ".dockerignore",
)


class ImageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ImageInfo:
    ref: str
    digest: str  # the image id, "sha256:<64 hex>": a digest of the image config
    size_bytes: int
    user: str
    revision: str | None = None  # the git commit it was built from (REVISION_LABEL)
    dirty: bool | None = None  # whether IMAGE_SOURCES differed from that commit


@dataclass(frozen=True, slots=True)
class SourceState:
    commit: str
    changed: tuple[str, ...]  # IMAGE_SOURCES paths that differ from the commit or are untracked

    @property
    def dirty(self) -> bool:
        return bool(self.changed)


def _git(args: list[str], cwd: Path) -> str:
    try:
        done = subprocess.run(  # noqa: S603 (fixed argv)
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ImageError(f"git {' '.join(args)} failed: {exc}") from exc
    return done.stdout


def source_state(context: Path = Path()) -> SourceState:
    """HEAD, and every image source path not exactly as committed (modified, staged,
    deleted or untracked; ignored files such as __pycache__ do not count)."""
    commit = _git(["rev-parse", "HEAD"], context).strip()
    status = _git(
        ["status", "--porcelain=v1", "--untracked-files=all", "--", *IMAGE_SOURCES], context
    )
    changed = tuple(sorted(line[3:] for line in status.splitlines() if line.strip()))
    return SourceState(commit=commit, changed=changed)


def package_version() -> str:
    return version("meridian-fleet")


def default_image() -> str:
    return f"{IMAGE_NAME}:{package_version()}"


def inspect(ref: str) -> ImageInfo:
    """docker image inspect (read only)."""
    try:
        done = subprocess.run(  # noqa: S603 (fixed argv)
            ["docker", "image", "inspect", ref],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ImageError(f"cannot inspect image {ref}: {exc}") from exc
    (data,) = json.loads(done.stdout)
    labels = dict(data.get("Config", {}).get("Labels") or {})
    dirty = labels.get(DIRTY_LABEL)
    return ImageInfo(
        ref=ref,
        digest=str(data["Id"]),
        size_bytes=int(data["Size"]),
        user=str(data.get("Config", {}).get("User", "")),
        revision=labels.get(REVISION_LABEL),
        dirty=None if dirty is None else dirty == "true",
    )


def build(ref: str, context: Path = Path(), dockerfile: Path = DOCKERFILE) -> ImageInfo:
    """docker build -f docker/agent.Dockerfile -t <ref> <context>, labelled with the commit
    it was built from and whether the image sources differed from it, then inspect."""
    state = source_state(context)
    labels = [
        "--label",
        f"{REVISION_LABEL}={state.commit}",
        "--label",
        f"{DIRTY_LABEL}={'true' if state.dirty else 'false'}",
    ]
    try:
        subprocess.run(  # noqa: S603 (fixed argv)
            ["docker", "build", "-f", str(dockerfile), *labels, "-t", ref, str(context)],  # noqa: S607
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ImageError(f"docker build failed: {exc}") from exc
    return inspect(ref)
