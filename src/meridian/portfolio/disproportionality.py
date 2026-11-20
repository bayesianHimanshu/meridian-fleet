"""Disproportionality in spontaneous reports (phase 5 step 3a), as pre-registered in
docs/signal-scout.md: the 2 x 2 table of reports, the proportional reporting ratio and
the reporting odds ratio with 95% confidence intervals, Yates's chi-squared, and the
Evans 2001 signal rule. Pure arithmetic; no data access."""

import math
from dataclasses import dataclass

Z95 = 1.959963984540054  # the 97.5th percentile of the standard normal


@dataclass(frozen=True, slots=True)
class Table:
    """a: drug and term; b: drug without term; c: term without drug; d: neither."""

    a: int
    b: int
    c: int
    d: int

    @classmethod
    def from_counts(cls, with_drug_and_term: int, drug: int, term: int, total: int) -> Table:
        a = with_drug_and_term
        b = drug - a
        c = term - a
        d = total - a - b - c
        if min(a, b, c, d) < 0:
            raise ValueError(f"inconsistent counts: a={a} drug={drug} term={term} total={total}")
        return cls(a, b, c, d)

    @property
    def n(self) -> int:
        return self.a + self.b + self.c + self.d


@dataclass(frozen=True, slots=True)
class Ratio:
    value: float
    low: float
    high: float


def _ci(value: float, se: float) -> Ratio:
    log = math.log(value)
    return Ratio(value, math.exp(log - Z95 * se), math.exp(log + Z95 * se))


def prr(t: Table) -> Ratio | None:
    """[a / (a + b)] / [c / (c + d)]; None when a or c is 0 (undefined)."""
    if t.a == 0 or t.c == 0 or t.a + t.b == 0 or t.c + t.d == 0:
        return None
    value = (t.a / (t.a + t.b)) / (t.c / (t.c + t.d))
    se = math.sqrt(1 / t.a - 1 / (t.a + t.b) + 1 / t.c - 1 / (t.c + t.d))
    return _ci(value, se)


def ror(t: Table) -> Ratio | None:
    """(a d) / (b c); None when any cell is 0."""
    if min(t.a, t.b, t.c, t.d) == 0:
        return None
    value = (t.a * t.d) / (t.b * t.c)
    se = math.sqrt(1 / t.a + 1 / t.b + 1 / t.c + 1 / t.d)
    return _ci(value, se)


def chi2_yates(t: Table) -> float:
    """Pearson's chi-squared with Yates's continuity correction, 1 degree of freedom. The
    correction is not allowed to cross zero (|ad - bc| below N/2 gives 0)."""
    n = t.n
    rows = (t.a + t.b) * (t.c + t.d)
    cols = (t.a + t.c) * (t.b + t.d)
    if rows == 0 or cols == 0:
        return 0.0
    diff = max(abs(t.a * t.d - t.b * t.c) - n / 2, 0.0)
    return n * diff * diff / (rows * cols)


@dataclass(frozen=True, slots=True)
class SignalRule:
    """Evans, Waller and Davis 2001: 3 or more cases, PRR at least 2, chi-squared at least
    4 (minimum criteria)."""

    min_reports: int = 3
    prr_min: float = 2.0
    chi2_min: float = 4.0

    def is_signal(self, t: Table) -> bool:
        p = prr(t)
        return (
            t.a >= self.min_reports
            and p is not None
            and p.value >= self.prr_min
            and chi2_yates(t) >= self.chi2_min
        )
