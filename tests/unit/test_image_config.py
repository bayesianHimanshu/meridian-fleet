"""Every config file the agent image copies is in the build context (.dockerignore) and in
IMAGE_SOURCES, so a change to it marks the image dirty (phase 5 step 2 added
config/ind_requirements.toml; the inventory under data/ is never in the image)."""

import re
from pathlib import Path

from meridian.sandbox.image import IMAGE_SOURCES

ROOT = Path(__file__).resolve().parents[2]


def _copied_config() -> set[str]:
    text = (ROOT / "docker" / "agent.Dockerfile").read_text().replace("\\\n", " ")
    files: set[str] = set()
    for line in text.splitlines():
        if line.strip().startswith("COPY") and "/opt/meridian/config/" in line:
            files |= set(re.findall(r"config/[a-z_]+\.toml", line))
    return files


def test_the_image_config_files_are_in_the_context_and_its_sources() -> None:
    copied = _copied_config()
    assert "config/ind_requirements.toml" in copied
    assert "config/translational_monitoring.toml" in copied
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    for f in copied:
        assert f"!{f}" in ignore, f
        assert f in IMAGE_SOURCES, f


def test_no_data_reaches_the_image() -> None:
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert ignore[0] == "*"
    assert not any(line.startswith("!data") for line in ignore)
