"""The step 4 detector versions flag exactly what the 2.x versions did on fixture cuts.

tests/fixtures/detector_flags_v2.json was captured with kri_monitor 2.1.0, qtl_detector
2.0.0 and csm 2.0.0 before any step 4 change (tests/detector_scenarios.py). The new
versions must give the same (kind, site, check) set and severity per scenario and agent,
and keep every old evidence item's statistic, value and threshold: only context and new
supporting items may be added.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.detector_scenarios import GOLDEN, SCENARIOS, run_all

AGENTS = ("rbqm.kri_monitor", "rbqm.qtl_detector", "rbqm.csm")


@pytest.fixture(scope="module")
def current(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, list[dict[str, Any]]]]:
    return run_all(tmp_path_factory.mktemp("scenarios"))


@pytest.fixture(scope="module")
def golden() -> dict[str, dict[str, list[dict[str, Any]]]]:
    data: dict[str, dict[str, list[dict[str, Any]]]] = json.loads(Path(GOLDEN).read_text())
    return data


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("agent", AGENTS)
def test_the_same_findings_are_flagged(
    current: dict[str, dict[str, list[dict[str, Any]]]],
    golden: dict[str, dict[str, list[dict[str, Any]]]],
    scenario: str,
    agent: str,
) -> None:
    new, old = current[scenario][agent], golden[scenario][agent]
    assert [f["key"] for f in new] == [f["key"] for f in old]
    assert [f["severity"] for f in new] == [f["severity"] for f in old]
    for n, o in zip(new, old, strict=True):
        assert [tuple(e) for e in o["evidence"]] == [
            tuple(e) for e in n["evidence"] if tuple(e) in {tuple(x) for x in o["evidence"]}
        ], "an old evidence item changed or moved"
        assert {tuple(e) for e in o["evidence"]} <= {tuple(e) for e in n["evidence"]}


def test_the_scenarios_flag_something_for_every_agent(
    golden: dict[str, dict[str, list[dict[str, Any]]]],
) -> None:
    for agent in AGENTS:
        assert any(golden[s][agent] for s in golden), agent
