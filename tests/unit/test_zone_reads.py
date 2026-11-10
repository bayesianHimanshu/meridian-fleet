"""Per-zone read allow-lists of the staged areas, and the safety.local_model policy."""

from pathlib import Path

import pytest

from meridian.platform.config import Settings
from meridian.sandbox import policies
from meridian.sandbox.staging import StagingError, cut_areas_for

REPO = Path(__file__).resolve().parents[2]
COMMITTED = REPO / "policies"
DOCKERFILE = REPO / "docker" / "agent.Dockerfile"


def _stage_paths(name: str) -> set[str]:
    text = (COMMITTED / f"{name}.yaml").read_text()
    return {line.strip()[2:] for line in text.splitlines() if line.strip().startswith("- /stage")}


@pytest.mark.parametrize("name", sorted(policies.ALL_POLICIES))
def test_each_policy_file_lists_exactly_its_reads(name: str) -> None:
    reads = policies.ALL_POLICIES[name].reads
    assert _stage_paths(name) == {f"/stage/{a}" for a in reads}
    assert set(reads) <= set(policies.STAGE_AREAS)
    assert "    - /stage\n" not in (COMMITTED / f"{name}.yaml").read_text()  # never the root


def test_only_the_unblinded_zone_may_read_the_unblinded_area() -> None:
    readers = sorted(n for n, p in policies.ALL_POLICIES.items() if "unblinded" in p.reads)
    assert readers == ["unblinded"]


def test_the_zones_read_what_their_agents_need_and_no_more() -> None:
    assert policies.ALL_POLICIES["rbqm"].reads == ("meta", "blinded", "ops")
    # The reconciler reads ae, dm and the safety database snapshot, all blinded.
    assert policies.ALL_POLICIES["safety"].reads == ("meta", "blinded")
    assert policies.ALL_POLICIES["safety.sae_reconciler"].reads == ("meta", "blinded")
    assert policies.ALL_POLICIES["rbqm.narrator"].reads == ("narr",)
    assert policies.ALL_POLICIES["safety.local_model"].reads == ("lm",)
    # quality.tmf_completeness reads blinded DM and the eTMF index; no operational data.
    assert policies.ALL_POLICIES["quality"].reads == ("meta", "blinded", "tmf")


def test_only_the_quality_zone_may_read_the_etmf_area() -> None:
    readers = sorted(n for n, p in policies.ALL_POLICIES.items() if "tmf" in p.reads)
    assert readers == ["quality"]
    assert policies.POLICY_BOUNDARY["quality"] == "quality"
    assert sorted(n for n, b in policies.BOUNDARIES.items() if "tmf" in b.reads) == ["quality"]


def test_a_cut_is_staged_only_with_the_areas_a_policy_reads() -> None:
    assert cut_areas_for(policies.ALL_POLICIES["rbqm"].reads) == ("blinded", "ops")
    assert cut_areas_for(policies.ALL_POLICIES["safety"].reads) == ("blinded",)
    with pytest.raises(StagingError):
        cut_areas_for(policies.ALL_POLICIES["safety.local_model"].reads)


def test_the_image_creates_every_stage_area() -> None:
    text = DOCKERFILE.read_text()
    assert "for area in " + " ".join(policies.STAGE_AREAS) + "; do" in text


def test_applied_hashes_now_differ_where_the_rules_differ() -> None:
    groups: dict[str, list[str]] = {}
    for name in policies.ALL_POLICIES:
        groups.setdefault(policies.applied_sha256(name), []).append(name)
    assert sorted(sorted(g) for g in groups.values()) == [
        ["portfolio", "preclinical", "rbqm", "study_ops"],
        ["quality"],
        ["rbqm.narrator"],
        ["safety", "safety.sae_reconciler"],
        ["safety.local_model"],
        ["unblinded"],
    ]


LOCAL = policies.ALL_POLICIES["safety.local_model"]


def test_the_local_model_policy_reaches_one_address_and_two_requests() -> None:
    assert (LOCAL.zone, LOCAL.providers) == ("safety_local", ())
    (rule,) = LOCAL.network
    assert (rule.host, rule.port, rule.protocol, rule.enforcement) == (
        "172.17.0.1",
        11434,
        "rest",
        "enforce",
    )
    assert rule.allowed_ips == ("172.17.0.1/32",)
    assert rule.rules == (
        policies.HttpRule("GET", "/api/tags"),
        policies.HttpRule("POST", "/api/chat"),
    )
    assert rule.binaries == ("/usr/local/bin/python3.14",)
    for agent in ("safety.term_adjudicator", "safety.local_narrator"):
        assert policies.policy_for_agent(agent) == "safety.local_model"


def test_the_local_model_expected_effective_is_what_0_1_7_reported() -> None:
    """Pinned to `openshell sandbox get --policy-only` for this policy (2026-11-09)."""
    effective = policies.expected_effective(LOCAL)
    lines = effective.splitlines()
    rw = lines[lines.index("  read_write:") + 1 : lines.index("landlock:")]
    assert [x.strip()[2:] for x in rw] == ["/tmp", "/stage/lm", "/dev/null"]
    tail = lines[lines.index("network_policies:") :]
    assert tail[4:7] == [
        "      - host: '172.17.0.1'",
        "        port: 11434",
        "        protocol: rest",
    ]
    assert tail[-3:] == [
        "          - '172.17.0.1/32'",
        "    binaries:",
        "      - path: /usr/local/bin/python3.14",
    ]
    assert not effective.endswith("\n")


def test_the_configured_base_url_is_the_policy_address() -> None:
    settings = Settings()
    assert settings.local_model is not None
    assert settings.local_model.base_url == f"http://{policies.OLLAMA_HOST}:{policies.OLLAMA_PORT}"
