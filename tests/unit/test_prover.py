"""meridian policy prove: zone policies against their boundary with openshell-prover.

Skipped with the reason when openshell-prover is not installed (CI downloads the pinned
0.1.7 release before running the tests).
"""

import shutil
from pathlib import Path

import pytest

from meridian.sandbox import policies
from meridian.sandbox.prover import ProverResult, check, prove_all

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "policies"
BOUNDARY = ROOT / "boundaries" / "blinded.yaml"
needs_prover = pytest.mark.skipif(
    shutil.which("openshell-prover") is None, reason="openshell-prover is not installed"
)


def _variant(tmp_path: Path, name: str, old: str, new: str) -> Path:
    """A test-only policy: the committed rbqm policy with one change, never under policies/."""
    text = (ROOT / "rbqm.yaml").read_text()
    assert old in text
    path = tmp_path / f"{name}.yaml"
    path.write_text(text.replace(old, new, 1))
    return path


def test_boundary_file_equals_its_rendering_and_every_zone_has_one() -> None:
    assert BOUNDARY.read_bytes() == policies.render_boundary("blinded").encode()
    assert set(policies.ZONE_BOUNDARY) == set(policies.POLICIES)
    assert set(policies.POLICY_BOUNDARY) == set(policies.ALL_POLICIES)
    assert set(policies.POLICY_BOUNDARY.values()) == set(policies.BOUNDARIES)
    for name in policies.BOUNDARIES:
        path = policies.boundary_path(name, ROOT)
        assert path.read_bytes() == policies.render_boundary(name).encode()


@needs_prover
def test_every_zone_is_within_its_boundary() -> None:
    results = prove_all(ROOT)
    assert set(results) == set(policies.ALL_POLICIES)
    for zone, result in results.items():
        assert result.passed, f"{zone}: {result.explain()}"
        assert {"filesystem", "process", "landlock"} <= set(result.domains)


@needs_prover
def test_an_extra_read_path_is_not_within_the_boundary(tmp_path: Path) -> None:
    candidate = _variant(
        tmp_path, "extra_read", "    - /usr/bin/tar\n", "    - /usr/bin/tar\n    - /opt/canary\n"
    )
    result = check(candidate, BOUNDARY)
    assert not result.passed
    assert result.result != "within_boundary"
    # On 0.1.7: unsupported, unresolved_filesystem_path, naming the path.
    assert "/opt/canary" in (result.reason or "") + str(result.counterexample)


@needs_prover
def test_run_as_root_exceeds_the_boundary(tmp_path: Path) -> None:
    candidate = _variant(tmp_path, "root", "run_as_user: sandbox", "run_as_user: root")
    result = check(candidate, BOUNDARY)
    assert result.result == "exceeds_boundary"
    assert result.counterexample == {
        "domain": "process",
        "field": "run_as_user",
        "boundary": "sandbox",
        "candidate": "root",
    }


@needs_prover
def test_landlock_best_effort_exceeds_the_boundary(tmp_path: Path) -> None:
    candidate = _variant(
        tmp_path, "best_effort", "compatibility: hard_requirement", "compatibility: best_effort"
    )
    result = check(candidate, BOUNDARY)
    assert result.result == "exceeds_boundary"
    assert result.counterexample == {
        "domain": "landlock",
        "boundary": "hard_requirement",
        "candidate": "best_effort",
    }


def _result(result: str, domains: tuple[str, ...]) -> ProverResult:
    return ProverResult(Path("c"), Path("b"), result, domains, None, None, None, "")


def test_pass_needs_within_boundary_and_full_coverage() -> None:
    full = ("filesystem", "network_l4", "process", "landlock")
    assert _result("within_boundary", full).passed
    assert not _result("within_boundary", ("filesystem", "process")).passed
    assert (
        "coverage lacks landlock" in _result("within_boundary", ("filesystem", "process")).explain()
    )
    for other in ("exceeds_boundary", "unsupported", "inconclusive", "error"):
        assert not _result(other, full).passed


def test_output_that_is_not_json_fails(tmp_path: Path) -> None:
    fake = tmp_path / "fake-prover"
    fake.write_text("#!/bin/sh\necho 'not json'\nexit 0\n")
    fake.chmod(0o755)
    result = check(ROOT / "rbqm.yaml", BOUNDARY, binary=str(fake))
    assert result.result == "error"
    assert not result.passed


NARRATOR_FILE = ROOT / "rbqm.narrator.yaml"
NARRATOR_BOUNDARY = ROOT / "boundaries" / "narrator.yaml"


def _narrator_variant(tmp_path: Path, name: str, old: str, new: str) -> Path:
    text = NARRATOR_FILE.read_text()
    assert old in text
    path = tmp_path / f"{name}.yaml"
    path.write_text(text.replace(old, new, 1))
    return path


@needs_prover
def test_the_narrator_policy_exceeds_the_blinded_boundary() -> None:
    """Egress is allowed only against the narrator boundary, never a zone's."""
    result = check(NARRATOR_FILE, BOUNDARY)
    assert result.result == "exceeds_boundary"


@needs_prover
def test_narrator_egress_to_another_host_exceeds_its_boundary(tmp_path: Path) -> None:
    candidate = _narrator_variant(tmp_path, "host", "api.anthropic.com", "example.com")
    result = check(candidate, NARRATOR_BOUNDARY)
    assert result.result == "exceeds_boundary"
    assert (result.counterexample or {}).get("host") == "example.com"


@needs_prover
def test_narrator_egress_for_another_binary_exceeds_its_boundary(tmp_path: Path) -> None:
    candidate = _narrator_variant(
        tmp_path, "binary", "path: /usr/local/bin/python3.14", "path: /usr/bin/bash"
    )
    assert check(candidate, NARRATOR_BOUNDARY).result != "within_boundary"


@needs_prover
def test_the_staged_areas_separate_the_zones() -> None:
    """Each boundary lists only the staged areas its policies may read, so a policy that
    reads another area is outside it, whichever way round."""
    boundary = policies.boundary_path
    outside = [
        ("rbqm.yaml", "narrator"),  # the cut areas; the narrator reads only its job
        ("rbqm.narrator.yaml", "blinded"),  # egress
        ("unblinded.yaml", "blinded"),  # /stage/unblinded
        ("rbqm.yaml", "local_model"),
        ("safety.local_model.yaml", "blinded"),  # egress to the local model
        ("safety.local_model.yaml", "narrator"),  # a different endpoint
    ]
    for policy, name in outside:
        result = check(ROOT / policy, boundary(name, ROOT))
        assert result.result != "within_boundary", f"{policy} vs {name}: {result.explain()}"
    assert check(ROOT / "safety.yaml", boundary("blinded", ROOT)).passed
