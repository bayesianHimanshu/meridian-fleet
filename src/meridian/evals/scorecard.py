"""Scorecards: one evaluation run, written once to data/evals/<run_id>.json."""

import hashlib
import json
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.domain.ids import UtcDatetime
from meridian.evals.matching import (
    FaultMatch,
    FindingRecord,
    VersionFilter,
    findings_for_cut,
    match_faults,
)
from meridian.evals.metrics import CutMetrics, Metrics, cut_metrics, metrics
from meridian.evals.rules import (
    RuleCondition,
    RuleEvaluation,
    evaluate_rules,
    is_rule_fault,
    is_rule_finding,
)
from meridian.faults.catalog import FaultRecord
from meridian.ledger.hashing import canonical_json
from meridian.ledger.store import LedgerReader
from meridian.platform.clock import Clock

Mode = Literal["faults", "null"]
Scoring = Literal["planted", "rules_and_detectors"]


class ScorecardExistsError(Exception):
    pass


class CutEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cut_id: str
    mode: Mode
    metrics: CutMetrics
    matches: tuple[FaultMatch, ...]
    findings: tuple[FindingRecord, ...]
    false_positive_ids: tuple[str, ...]
    # Step 5 scoring. Scorecards written before step 5 have the defaults ("planted").
    # metrics, matches and false_positive_ids above stay the step 4 scoring of every
    # finding against planted faults, so the original criteria can still be reported.
    scoring: Scoring = "planted"
    detector_metrics: Metrics | None = None
    detector_matches: tuple[FaultMatch, ...] = ()
    detector_false_positive_ids: tuple[str, ...] = ()
    condition_truth: tuple[RuleCondition, ...] = ()
    rules: RuleEvaluation | None = None


class Scorecard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    created_at: UtcDatetime
    git_commit: str
    version_filter: VersionFilter
    agent_versions: str
    config_hash: str
    cuts: tuple[str, ...]
    evaluations: tuple[CutEvaluation, ...]

    def evaluation(self, cut_id: str) -> CutEvaluation:
        for e in self.evaluations:
            if e.cut_id == cut_id:
                return e
        raise KeyError(cut_id)


def git_commit(cwd: Path | None = None) -> str:
    """The checked-out commit, read with `git rev-parse HEAD` (read only)."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 (git on PATH is the point)
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    return done.stdout.strip()


def config_hash(agents_config: object) -> str:
    return hashlib.sha256(canonical_json(agents_config).encode("utf-8")).hexdigest()


def evaluate_cut(
    reader: LedgerReader,
    cut_id: str,
    faults: Sequence[FaultRecord] | None,
    versions: VersionFilter,
    agents: Sequence[str],
    conditions: Sequence[RuleCondition] | None = None,
) -> CutEvaluation:
    """faults=None means a null cut: there is nothing to find, every finding is a FP.

    With conditions (condition truth of the cut), the cut is also scored the step 5 way:
    rule findings against condition truth, detector findings against detector faults.
    """
    findings = findings_for_cut(reader, cut_id, versions)
    planted = list(faults or [])
    result = match_faults(planted, findings)
    base = CutEvaluation(
        cut_id=cut_id,
        mode="null" if faults is None else "faults",
        metrics=cut_metrics(planted, findings, result, agents),
        matches=result.matches,
        findings=tuple(findings),
        false_positive_ids=result.false_positives,
    )
    if conditions is None:
        return base
    detector_faults = [f for f in planted if not is_rule_fault(f)]
    detector_findings = [f for f in findings if not is_rule_finding(f)]
    detected = match_faults(detector_faults, detector_findings)
    return base.model_copy(
        update={
            "scoring": "rules_and_detectors",
            "detector_metrics": metrics(
                detector_findings, detected.matches, set(detected.true_positives)
            ),
            "detector_matches": detected.matches,
            "detector_false_positive_ids": detected.false_positives,
            "condition_truth": tuple(conditions),
            "rules": evaluate_rules(conditions, findings, planted),
        }
    )


def new_scorecard(
    *,
    clock: Clock,
    evaluations: Sequence[CutEvaluation],
    versions: VersionFilter,
    agents_config: object,
    git_sha: str,
) -> Scorecard:
    return Scorecard(
        run_id=str(uuid.uuid7()),
        created_at=clock.now(),
        git_commit=git_sha,
        version_filter=versions,
        agent_versions=versions.label(),
        config_hash=config_hash(agents_config),
        cuts=tuple(e.cut_id for e in evaluations),
        evaluations=tuple(evaluations),
    )


@dataclass(frozen=True, slots=True)
class ScorecardStore:
    root: Path

    def write(self, card: Scorecard) -> Path:
        """Append-only: refuses to write a run_id that already exists."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{card.run_id}.json"
        text = json.dumps(card.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError as exc:
            raise ScorecardExistsError(f"scorecard {card.run_id} already exists") from exc
        return path

    def all(self) -> list[Scorecard]:
        if not self.root.exists():
            return []
        cards = [Scorecard.model_validate_json(p.read_bytes()) for p in self.root.glob("*.json")]
        return sorted(cards, key=lambda c: (c.created_at, c.run_id))

    def latest(self, cut_id: str, version_label: str) -> CutEvaluation | None:
        """The most recent evaluation of cut_id under a version filter label."""
        for card in reversed(self.all()):
            if card.agent_versions == version_label and cut_id in card.cuts:
                return card.evaluation(cut_id)
        return None
