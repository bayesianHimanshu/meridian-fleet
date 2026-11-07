"""Every check an agent can raise, typed as a rule or a detector. The one place this lives.

rule: a threshold on a measured value defines the breach. The finding is right exactly
when the measured value crosses the threshold, so it is scored against condition truth
computed from the cut itself.

detector: a statistical test that can be wrong. It is scored against planted faults.
"""

from enum import StrEnum


class CheckType(StrEnum):
    RULE = "rule"
    DETECTOR = "detector"


class UnknownCheckError(KeyError):
    pass


CHECK_TYPES: dict[str, CheckType] = {
    "data_entry_lag": CheckType.RULE,
    "query_aging": CheckType.RULE,
    "premature_discontinuation": CheckType.RULE,
    "ae_rate": CheckType.DETECTOR,
    "deviations": CheckType.DETECTOR,
    "variance": CheckType.DETECTOR,
    "terminal_digit": CheckType.DETECTOR,
}


def check_type(check: str) -> CheckType:
    try:
        return CHECK_TYPES[check]
    except KeyError:
        known = sorted(CHECK_TYPES)
        raise UnknownCheckError(f"check {check!r} has no check type; known: {known}") from None


RULE_CHECKS: tuple[str, ...] = tuple(c for c, t in CHECK_TYPES.items() if t is CheckType.RULE)
