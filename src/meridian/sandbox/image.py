"""The agent image: build it with docker and read back its digest and size."""

import json
import subprocess
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

DOCKERFILE = Path("docker/agent.Dockerfile")
IMAGE_NAME = "meridian-agent"


class ImageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ImageInfo:
    ref: str
    digest: str  # the image id, "sha256:<64 hex>": a digest of the image config
    size_bytes: int
    user: str


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
    return ImageInfo(
        ref=ref,
        digest=str(data["Id"]),
        size_bytes=int(data["Size"]),
        user=str(data.get("Config", {}).get("User", "")),
    )


def build(ref: str, context: Path = Path(), dockerfile: Path = DOCKERFILE) -> ImageInfo:
    """docker build -f docker/agent.Dockerfile -t <ref> <context>, then inspect."""
    try:
        subprocess.run(  # noqa: S603 (fixed argv)
            ["docker", "build", "-f", str(dockerfile), "-t", ref, str(context)],  # noqa: S607
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ImageError(f"docker build failed: {exc}") from exc
    return inspect(ref)
