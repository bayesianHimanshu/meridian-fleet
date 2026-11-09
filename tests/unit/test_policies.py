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
    "/stage/meta",
    "/stage/blinded",
    "/stage/ops",
}


def test_every_zone_has_a_policy_and_two_agents_have_their_own() -> None:
    assert set(policies.POLICIES) == {z.value for z in Zone}
    assert set(policies.AGENT_POLICIES) == {
        "rbqm.narrator",
        "safety.sae_reconciler",
        "safety.local_model",
    }
    assert sorted(p.name.removesuffix(".yaml") for p in COMMITTED.glob("*.yaml")) == sorted(
        policies.ALL_POLICIES
    )


@pytest.mark.parametrize("name", sorted(policies.ALL_POLICIES))
def test_rendered_policy_equals_the_committed_file(name: str) -> None:
    rendered = policies.render(policies.ALL_POLICIES[name])
    assert rendered.encode() == (COMMITTED / f"{name}.yaml").read_bytes()


def test_render_all_writes_the_same_bytes(tmp_path: Path) -> None:
    written = policies.render_all(tmp_path)
    assert len(written) == len(policies.ALL_POLICIES) + len(policies.BOUNDARIES)
    for path in written:
        assert path.read_bytes() == (COMMITTED / path.relative_to(tmp_path)).read_bytes()


def test_the_provider_profile_equals_its_rendering(tmp_path: Path) -> None:
    (written,) = policies.render_profiles(tmp_path)
    assert written.read_bytes() == (REPO / "profiles" / "anthropic.yaml").read_bytes()
    text = written.read_text()
    assert "sk-ant-" not in text  # no secret, not even a prefix
    assert "      - allow:\n          method: POST\n          path: /v1/messages" in text
    assert "  - /usr/local/bin/python3.14" in text


@pytest.mark.parametrize("zone", sorted(policies.POLICIES))
def test_zone_policy_lists_only_allowed_paths_and_no_network(zone: str) -> None:
    text = (COMMITTED / f"{zone}.yaml").read_text()
    listed = {line.strip()[2:] for line in text.splitlines() if line.strip().startswith("- ")}
    allowed = ALLOWED | ({"/stage/unblinded"} if zone == "unblinded" else set())
    assert listed <= allowed, listed - allowed
    if zone != "unblinded":
        assert "/unblinded" not in text
    assert "ground_truth" not in text
    policy = policies.POLICIES[zone]
    assert policy.read_write == ("/tmp", *(f"/stage/{a}" for a in policies.ZONE_READS[zone]))
    assert "/" not in policy.read_only
    assert policy.landlock == "hard_requirement"
    assert policy.network == ()
    assert "network_policies: {}" in text.splitlines()
    assert "include_workdir: false" in text


NARRATOR = policies.ALL_POLICIES["rbqm.narrator"]


def test_the_narrator_policy_is_rbqm_plus_one_endpoint_for_python() -> None:
    rbqm = policies.POLICIES["rbqm"]
    # The rbqm read-only paths, and of the staged areas only its own job folder.
    assert (NARRATOR.zone, NARRATOR.read_only, NARRATOR.read_write) == (
        "rbqm",
        rbqm.read_only,
        ("/tmp", "/stage/narr"),
    )
    (rule,) = NARRATOR.network
    assert (rule.host, rule.port, rule.protocol, rule.enforcement) == (
        "api.anthropic.com",
        443,
        "rest",
        "enforce",
    )
    assert rule.rules == (policies.HttpRule("POST", "/v1/messages"),)
    assert rule.binaries == ("/usr/local/bin/python3.14",)
    assert NARRATOR.providers == ("meridian-anthropic",)
    text = (COMMITTED / "rbqm.narrator.yaml").read_text()
    listed = {
        line.strip()[2:]
        for line in text.split("network_policies:")[0].splitlines()
        if line.strip().startswith("- ")
    }
    assert listed <= (ALLOWED - {"/stage/meta", "/stage/blinded", "/stage/ops"}) | {"/stage/narr"}


def test_narrator_expected_effective_adds_baseline_and_provider_rule() -> None:
    """Pinned to what 0.1.7 reported for the narrator policy with the provider attached."""
    effective = policies.expected_effective(NARRATOR)
    lines = effective.splitlines()
    ro = lines[lines.index("  read_only:") + 1 : lines.index("  read_write:")]
    assert [x.strip()[2:] for x in ro] == [
        *NARRATOR.read_only,
        "/usr",
        "/lib",
        "/etc",
        "/var/log",
        "/proc",
        "/dev/urandom",
    ]
    assert "    - /dev/null" in lines
    keys = [x.strip()[:-1] for x in lines if x.startswith("  ") and not x.startswith("   ")]
    assert keys[-2:] == ["_provider_meridian_anthropic", "anthropic_messages"]
    assert "              path: /v1/models" in lines  # only in the provider's rule
    assert not effective.endswith("\n")
    assert "#" not in effective


def test_expected_effective_drops_comments_and_empty_network() -> None:
    effective = policies.expected_effective(policies.POLICIES["rbqm"])
    assert not effective.endswith("\n")
    assert "#" not in effective
    assert "network_policies" not in effective
    assert effective.startswith("version: 1\nfilesystem_policy:")


def test_the_reconciler_policy_is_the_safety_zone_shape_without_network() -> None:
    mine = policies.ALL_POLICIES["safety.sae_reconciler"]
    zone = policies.POLICIES["safety"]
    assert (mine.zone, mine.network, mine.providers) == ("safety", (), ())
    assert (mine.read_only, mine.read_write, mine.landlock) == (
        zone.read_only,
        zone.read_write,
        zone.landlock,
    )
    assert policies.POLICY_BOUNDARY["safety.sae_reconciler"] == "blinded"
    text = (COMMITTED / "safety.sae_reconciler.yaml").read_text(encoding="utf-8")
    assert "network_policies: {}" in text
    assert "No provider is attached" in text
    assert policies.policy_for_agent("safety.sae_reconciler") == "safety.sae_reconciler"
    assert policies.policy_for_agent("rbqm.csm") == "rbqm"
