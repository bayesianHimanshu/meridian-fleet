"""Rules versus detectors in the eval (phase 1 step 5).

Rule checks are scored against condition truth: which sites (or the study) meet the
rule's breach condition in the cut itself, computed with the same shared functions the
agents use. Detector checks keep being scored against planted faults.
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.data.study_store import StudyReader
from meridian.evals.matching import FindingRecord
from meridian.faults.catalog import FaultRecord
from meridian.platform.agent_settings import KriMonitorConfig, QtlDetectorConfig
from meridian.stats.checks import CheckType, UnknownCheckError, check_type
from meridian.stats.rules import discontinuation, entry_lag, query_aging

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# The check each fault type is planted for. Plain strings: evals does not import agents.
FAULT_CHECK: dict[str, str] = {
    "fabricated_vitals": "variance",
    "ae_under_reporting": "ae_rate",
    "deviation_cluster": "deviations",
    "entry_lag_spike": "data_entry_lag",
    "query_backlog": "query_aging",
    "discontinuation_drift": "premature_discontinuation",
}


def fault_check(fault_type: str) -> str:
    try:
        return FAULT_CHECK[fault_type]
    except KeyError:
        raise UnknownCheckError(f"fault type {fault_type!r} has no check") from None


def is_rule_fault(fault: FaultRecord) -> bool:
    return check_type(fault_check(fault.fault_type)) is CheckType.RULE


def is_rule_finding(finding: FindingRecord) -> bool:
    return check_type(finding.check) is CheckType.RULE


class RuleCondition(BaseModel):
    """One unit of a rule check in one cut: the measured value and whether it breaches."""

    model_config = _FROZEN

    check: str
    site: str
    value: float | None
    n: int
    threshold: float
    met: bool


Outcome = Literal[
    "planted_breach",  # condition met at a planted fault, finding raised
    "natural_breach",  # condition met with no planted fault, finding raised: correct
    "below_threshold_by_design",  # planted fault, condition not met, no finding
    "missing_finding",  # condition met, no finding: an error
    "unsupported_finding",  # finding raised, condition not met: an error
]
ERRORS: frozenset[str] = frozenset({"missing_finding", "unsupported_finding"})


class RuleOutcome(BaseModel):
    model_config = _FROZEN

    check: str
    site: str
    outcome: Outcome
    value: float | None
    threshold: float | None
    finding_id: str | None
    fault_id: str | None


class RuleEvaluation(BaseModel):
    model_config = _FROZEN

    units: int  # every (check, site) with a measured value, plus any stray finding
    agreeing: int
    outcomes: tuple[RuleOutcome, ...]  # every unit with a breach, a finding or a fault

    @property
    def agreement(self) -> float:
        return self.agreeing / self.units if self.units else 1.0

    def of(self, outcome: Outcome) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.outcome == outcome]


def condition_truth(
    study: StudyReader, kri: KriMonitorConfig, qtl: QtlDetectorConfig
) -> tuple[RuleCondition, ...]:
    dm = study.table("dm")
    values = [
        *entry_lag(dm, study.table("data_entry"), kri.lag_days),
        *query_aging(dm, study.table("queries"), kri.query_age_days, kri.min_open_queries),
        discontinuation(dm, study.table("ds"), qtl.discontinuation_limit, qtl.approach_band),
    ]
    return tuple(
        RuleCondition(
            check=v.check, site=v.site, value=v.value, n=v.n, threshold=v.threshold, met=v.met
        )
        for v in values
    )


def evaluate_rules(
    conditions: Sequence[RuleCondition],
    findings: Sequence[FindingRecord],
    faults: Sequence[FaultRecord],
) -> RuleEvaluation:
    """Score rule findings against condition truth; planted rule faults only label units."""
    truth = {(c.check, c.site): c for c in conditions}
    found = {(f.check, f.site): f for f in findings if is_rule_finding(f)}
    planted = {(fault_check(f.fault_type), f.target): f for f in faults if is_rule_fault(f)}
    units = set(truth) | set(found)
    outcomes: list[RuleOutcome] = []
    agreeing = 0
    for unit in sorted(units | set(planted)):
        cond, finding, fault = truth.get(unit), found.get(unit), planted.get(unit)
        met = cond is not None and cond.met
        if met == (finding is not None) and unit in units:
            agreeing += 1
        outcome: Outcome | None
        if met and finding is not None:
            outcome = "planted_breach" if fault is not None else "natural_breach"
        elif met:
            outcome = "missing_finding"
        elif finding is not None:
            outcome = "unsupported_finding"
        elif fault is not None:
            outcome = "below_threshold_by_design"
        else:
            outcome = None
        if outcome is None:
            continue
        outcomes.append(
            RuleOutcome(
                check=unit[0],
                site=unit[1],
                outcome=outcome,
                value=cond.value if cond else None,
                threshold=cond.threshold if cond else None,
                finding_id=finding.finding_id if finding else None,
                fault_id=fault.fault_id if fault else None,
            )
        )
    return RuleEvaluation(units=len(units), agreeing=agreeing, outcomes=tuple(outcomes))
