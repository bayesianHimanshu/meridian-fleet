"""Zone policies: rendered from Python, committed, and checked."""

from pathlib import Path

import pytest

from meridian.agents.base import Zone
from meridian.sandbox import policies

REPO = Path(__file__).resolve().parents[2]
COMMITTED = REPO / "policies"
# Every path a policy may list. Anything else (or anything under /cut/unblinded) fails.
ALLOWED = {
    "/usr/local/bin/python3.14",
    "/usr/local/lib",
    "/usr/lib/x86_64-linux-gnu",
    "/opt/meridian",
    "/usr/bin/bash",
    "/usr/bin/cat",
    "/usr/bin/mkdir",
    "/usr/bin/tar",
    "/tmp",
}


def test_every_zone_has_a_policy() -> None:
    assert set(policies.POLICIES) == {z.value for z in Zone}
    assert sorted(p.stem for p in COMMITTED.glob("*.yaml")) == sorted(z.value for z in Zone)


@pytest.mark.parametrize("zone", sorted(policies.POLICIES))
def test_rendered_policy_equals_the_committed_file(zone: str) -> None:
    rendered = policies.render(policies.POLICIES[zone])
    assert rendered.encode() == (COMMITTED / f"{zone}.yaml").read_bytes()


def test_render_all_writes_the_same_bytes(tmp_path: Path) -> None:
    for path in policies.render_all(tmp_path):
        assert path.read_bytes() == (COMMITTED / path.name).read_bytes()


@pytest.mark.parametrize("zone", sorted(policies.POLICIES))
def test_policy_lists_only_allowed_paths_and_no_network(zone: str) -> None:
    text = (COMMITTED / f"{zone}.yaml").read_text()
    listed = {line.strip()[2:] for line in text.splitlines() if line.strip().startswith("- ")}
    assert listed <= ALLOWED, listed - ALLOWED
    assert "/unblinded" not in text  # the zone called unblinded may name itself
    assert "ground_truth" not in text
    policy = policies.POLICIES[zone]
    assert policy.read_write == ("/tmp",)
    assert "/" not in policy.read_only
    assert policy.landlock == "hard_requirement"
    assert policy.network == ()
    assert "network_policies: {}" in text.splitlines()
    assert "include_workdir: false" in text


def test_network_rules_cannot_be_rendered() -> None:
    with pytest.raises(ValueError, match="network"):
        policies.render(policies.ZonePolicy("rbqm", network=("pypi",)))


def test_expected_effective_drops_comments_and_empty_network() -> None:
    effective = policies.expected_effective(policies.POLICIES["rbqm"])
    assert not effective.endswith("\n")
    assert "#" not in effective
    assert "network_policies" not in effective
    assert effective.startswith("version: 1\nfilesystem_policy:")
