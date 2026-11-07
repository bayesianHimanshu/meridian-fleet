"""Planted fixture cuts on which the detector versions must flag the same things.

tests/fixtures/detector_flags_v2.json holds what kri_monitor 2.1.0, qtl_detector 2.0.0 and
csm 2.0.0 drafted on these scenarios, captured before step 4 changed any detector:

    uv run python -m tests.detector_scenarios --write

The equivalence test runs the current versions on the same scenarios and requires the
same (kind, site, check) set per scenario and agent, and every evidence item of the old
version unchanged (statistic, value, threshold). New versions may only add context.
"""

import argparse
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from meridian.agents.base import Agent, AgentConfig, FindingDraft, parse_dedup_key
from meridian.agents.rbqm.csm import Csm, CsmConfig
from meridian.agents.rbqm.kri_monitor import KriMonitor, KriMonitorConfig
from meridian.agents.rbqm.qtl_detector import QtlDetector, QtlDetectorConfig
from meridian.data.derived import premature_discontinuation
from tests.agent_support import Tables, clean, plant, run_agent
from tests.rbqm_support import make_tables

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "detector_flags_v2.json"
FABRICATE = {"subject_share": 0.8, "sd_scale": 0.25, "terminal_zero_share": 0.4}
Scenario = Callable[[], tuple[Tables, Tables, Tables]]


def _kri_all() -> tuple[Tables, Tables, Tables]:
    blinded, ops, unblinded = clean()
    for fault, site, params in (
        ("ae_under_reporting", "1003", {"drop_share": 0.7}),
        ("deviation_cluster", "1004", {"rate_multiplier": 4.0}),
        ("entry_lag_spike", "1002", {"target_median_days": 14.0}),
        ("query_backlog", "1005", {"target_median_age_days": 40.0}),
    ):
        blinded, ops = plant(blinded, ops, fault, site, params)
    return blinded, ops, unblinded


def _base_rate() -> float:
    blinded, _, _ = clean()
    discontinued, treated = premature_discontinuation(blinded["dm"], blinded["ds"])
    return discontinued / treated


def _qtl_drift() -> tuple[Tables, Tables, Tables]:
    blinded, ops, unblinded = clean()
    target = {"target_rate": _base_rate() + 0.1}
    return (*plant(blinded, ops, "discontinuation_drift", "STUDY", target), unblinded)


def _csm_two() -> tuple[Tables, Tables, Tables]:
    blinded, ops, unblinded = make_tables(n_sites=12, per_site=15, seed=7)
    for site in ("1003", "1009"):
        blinded, ops = plant(blinded, ops, "fabricated_vitals", site, FABRICATE)
    return blinded, ops, unblinded


def _twelve_clean() -> tuple[Tables, Tables, Tables]:
    return make_tables(n_sites=12, per_site=15, seed=7)


SCENARIOS: dict[str, Scenario] = {
    "clean": clean,
    "kri_all_four": _kri_all,
    "qtl_drift": _qtl_drift,
    "csm_two_sites": _csm_two,
    "csm_clean_twelve": _twelve_clean,
}


def _agents() -> list[tuple[Agent[Any], AgentConfig]]:
    return [
        (KriMonitor(), KriMonitorConfig()),
        # Between the clean base and the planted drift, as in test_rbqm_agents.
        (QtlDetector(), QtlDetectorConfig(discontinuation_limit=round(_base_rate() + 0.05, 3))),
        (Csm(), CsmConfig()),
    ]


def flagged(drafts: list[FindingDraft]) -> list[dict[str, Any]]:
    out = []
    for d in drafts:
        parts = parse_dedup_key(d.dedup_key)
        out.append(
            {
                "key": [d.kind.value, d.site_id or "STUDY", parts.check],
                "severity": d.severity.value,
                "evidence": [[e.statistic, e.value, e.threshold] for e in d.evidence],
            }
        )
    return sorted(out, key=lambda f: f["key"])


def run_all(root: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for name, scenario in SCENARIOS.items():
        blinded, ops, unblinded = scenario()
        result[name] = {}
        for agent, config in _agents():
            where = root / name / agent.agent_id
            drafts = run_agent(agent, config, where, "F", blinded, ops, unblinded)
            result[name][agent.agent_id] = flagged(drafts)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        data = run_all(Path(tmp))
    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    if args.write:
        GOLDEN.write_text(text)
    print(text[:2000])


if __name__ == "__main__":
    main()
