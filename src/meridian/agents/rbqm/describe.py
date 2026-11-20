"""How RBQM evidence is read: role, direction, reference value and display, per statistic.

A pure function of the evidence items, so the detectors (kri_monitor 2.2.0, qtl_detector
2.1.0, csm 2.1.0) and the narrator evaluation (which describes legacy findings the same
way) agree. Describing never changes a value or threshold, so it never changes what is
flagged.

Displays are self-contained phrases at a fixed precision per statistic. p values below
1e-12 read "below 1e-12" (a value of 0.0 is an underflow, not a certainty) and every p
value has 2 significant digits. A ratio the agent computed (value / threshold, value /
limit) is its own supporting item, so a narrative may only state a ratio that exists.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from meridian.domain.findings import Evidence, EvidenceDirection, EvidenceRole

P_FLOOR = 1e-12
CHANCE_TERMINAL_ZERO = 0.1  # a uniform last digit ends in 0 one time in ten
RATIO_STAT = "ratio_to_threshold"
SHARE_OF_LIMIT_STAT = "premature_discontinuation:share_of_limit"


def fmt_p(p: float) -> str:
    """A p value at 2 significant digits, floored at 1e-12."""
    if p < P_FLOOR:
        return "below 1e-12"
    if p >= 1e-3:
        return f"{float(f'{p:.2g}'):g}" if p < 1 else "1"
    mantissa, exp = f"{p:.1e}".split("e")
    return f"{mantissa}e-{int(exp[1:]):02d}"


def _p_phrase(label: str, p: float) -> str:
    shown = fmt_p(p)
    return f"{label} {shown}" if shown.startswith("below") else f"{label} = {shown}"


def _g(x: float) -> str:
    return f"{x:g}"


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


@dataclass(frozen=True, slots=True)
class _Rule:
    role: EvidenceRole
    # (value, threshold, expected) -> display
    display: Callable[[float, float | None, float | None], str]
    # The reference the value is judged against; None for a count or a p value.
    expected: Callable[[float | None], float | None] = lambda threshold: None


def _thr(text: str, threshold: float | None, unit: str = "") -> str:
    return text if threshold is None else f"{text} (threshold {_g(threshold)}{unit})"


def _with_expected(text: str, expected: float | None, fmt: Callable[[float], str]) -> str:
    return text if expected is None else f"{text} ({fmt(expected)} expected)"


def _p_rule(label: str, role: EvidenceRole) -> _Rule:
    return _Rule(role, lambda v, t, e: _thr(_p_phrase(label, v), t))


# Rate-test statistics share a check prefix (ae_rate, deviations); keys are the suffix.
_RATE: dict[str, _Rule] = {
    "observed": _Rule(
        "primary", lambda v, t, e: _with_expected(f"{v:.0f} observed", e, lambda x: f"{x:.1f}")
    ),
    "expected": _Rule("primary", lambda v, t, e: f"{v:.1f} expected from the rest of the study"),
    "phi": _Rule("supporting", lambda v, t, e: f"dispersion phi = {v:.2f}"),
    "z": _Rule("supporting", lambda v, t, e: f"z = {v:.2f}", lambda t: 0.0),
    "p": _p_rule("p", "supporting"),
    "poisson_p": _p_rule("Poisson p", "supporting"),
    "p_adjusted": _p_rule("Bonferroni-adjusted p", "primary"),
    "poisson_p_adjusted": _p_rule("Bonferroni-adjusted Poisson p", "primary"),
    "excluded_subjects": _Rule("supporting", lambda v, t, e: f"{v:.0f} subjects excluded"),
    "per_100_subject_days": _Rule(
        "supporting",
        lambda v, t, e: _with_expected(f"{v:.2f} per 100 subject-days", e, lambda x: f"{x:.2f}"),
        lambda t: t,
    ),
}
_FIXED: dict[str, _Rule] = {
    "data_entry_lag:median_days": _Rule(
        "primary", lambda v, t, e: _thr(f"median entry lag {v:.1f} days", t, " days"), lambda t: t
    ),
    "data_entry_lag:entries": _Rule("supporting", lambda v, t, e: f"{v:.0f} visits"),
    "query_aging:median_open_age_days": _Rule(
        "primary",
        lambda v, t, e: _thr(f"median open query age {v:.1f} days", t, " days"),
        lambda t: t,
    ),
    "query_aging:open_queries": _Rule("supporting", lambda v, t, e: f"{v:.0f} open queries"),
    "premature_discontinuation:rate": _Rule(
        "primary",
        lambda v, t, e: f"{_pct(v)} discontinued" + ("" if t is None else f" (QTL {_pct(t)})"),
        lambda t: t,
    ),
    "premature_discontinuation:discontinued": _Rule(
        "supporting", lambda v, t, e: f"{v:.0f} discontinued"
    ),
    "premature_discontinuation:treated": _Rule("supporting", lambda v, t, e: f"{v:.0f} treated"),
    "premature_discontinuation:approach_from": _Rule(
        "supporting", lambda v, t, e: f"approaching starts at {_pct(v)}"
    ),
    SHARE_OF_LIMIT_STAT: _Rule(
        "supporting",
        lambda v, t, e: (
            f"{_pct(v)} of the QTL" + ("" if t is None else f" (approaching from {100 * t:g}%)")
        ),
        lambda t: 1.0,
    ),
}
# Vital signs: per parameter (sysbp, diabp, pulse); keys are the suffix.
_VITAL: dict[str, _Rule] = {
    "log_sd_z": _Rule(
        "primary", lambda v, t, e: _thr(f"robust z of log SD = {v:.2f}", t), lambda t: 0.0
    ),
    "sd_ratio": _Rule(
        "primary", lambda v, t, e: _thr(f"SD {v:.2f} times the median site", t), lambda t: 1.0
    ),
    "terminal_zero_share": _Rule(
        "supporting",
        lambda v, t, e: f"{_pct(v)} of readings end in 0 ({_pct(CHANCE_TERMINAL_ZERO)} by chance)",
        lambda t: CHANCE_TERMINAL_ZERO,
    ),
    "terminal_zero_z": _Rule(
        "supporting",
        lambda v, t, e: _thr(f"z of the terminal-zero share = {v:.2f}", t),
        lambda t: 0.0,
    ),
    # csm 1.0.0 (legacy): the terminal-digit test was a finding's own basis then.
    "terminal_digit_chi2_p": _p_rule("terminal-digit test p", "primary"),
    "brown_forsythe_p": _p_rule("variance test p", "primary"),
}
_RATIO = _Rule("supporting", lambda v, t, e: f"{v:.2f} times the threshold", lambda t: 1.0)


def _rule(statistic: str) -> _Rule | None:
    if statistic in _FIXED:
        return _FIXED[statistic]
    prefix, _, suffix = statistic.partition(":")
    if suffix == RATIO_STAT:
        return _RATIO
    if prefix in ("ae_rate", "deviations"):
        return _RATE.get(suffix)
    if prefix in ("sysbp", "diabp", "pulse"):
        return _VITAL.get(suffix)
    return None


def _direction(value: float, expected: float | None) -> EvidenceDirection:
    if expected is None:  # a p value or a count has no reference
        return "none"
    if value < expected:
        return "low"
    return "high" if value > expected else "none"


def describe_one(item: Evidence, expected_count: float | None = None) -> Evidence:
    """The item with role, direction, expected and display filled in (values unchanged).

    An unknown statistic is returned as it is.
    """
    rule = _rule(item.statistic)
    if rule is None:
        return item
    expected = rule.expected(item.threshold)
    if item.statistic.endswith(":observed"):
        expected = expected_count
    return item.model_copy(
        update={
            "role": rule.role,
            "direction": _direction(item.value, expected),
            "expected": expected,
            "display": rule.display(item.value, item.threshold, expected),
        }
    )


def describe(items: Sequence[Evidence]) -> tuple[Evidence, ...]:
    """Every item described; an observed count is judged against its expected count."""
    expected_counts = {
        e.statistic.split(":", 1)[0]: e.value for e in items if e.statistic.endswith(":expected")
    }
    return tuple(describe_one(e, expected_counts.get(e.statistic.split(":", 1)[0])) for e in items)


def ratio_evidence(
    source: str, selector: str, check: str, value: float, threshold: float
) -> Evidence:
    """value / threshold as its own supporting item, as the agent computed it."""
    return Evidence(
        source=source,
        selector=selector,
        statistic=f"{check}:{RATIO_STAT}",
        value=value / threshold,
        threshold=None,
    )


def share_of_limit_evidence(source: str, selector: str, rate: float, limit: float) -> Evidence:
    return Evidence(
        source=source, selector=selector, statistic=SHARE_OF_LIMIT_STAT, value=rate / limit
    )
