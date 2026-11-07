"""Robust z-scores and the quasi-Poisson site test."""

import math
from collections.abc import Mapping
from dataclasses import dataclass

from scipy import stats

MAD_SCALE = 1.4826
"""Makes the MAD a consistent estimate of the SD for normal data."""
CHI2_1_MEDIAN = 0.4549
"""Median of a chi-square with 1 degree of freedom."""


def median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        raise ValueError("median of no values")
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def robust_z(values: Mapping[str, float]) -> dict[str, float] | None:
    """z = (x - median) / (1.4826 * MAD) per key. None when the MAD is 0 (or no values)."""
    if not values:
        return None
    xs = list(values.values())
    center = median(xs)
    mad = median([abs(x - center) for x in xs])
    if mad == 0:
        return None
    return {k: (v - center) / (MAD_SCALE * mad) for k, v in values.items()}


@dataclass(frozen=True, slots=True)
class QuasiPoissonSite:
    site: str
    observed: int
    expected: float
    rate_others: float
    residual: float  # (O - E) / sqrt(E), against the leave-one-out expectation
    z: float
    p: float


@dataclass(frozen=True, slots=True)
class QuasiPoissonResult:
    phi: float
    reference_rate: float  # median site rate, the reference phi is estimated against
    sites: tuple[QuasiPoissonSite, ...]


def dispersion(residuals: list[float]) -> float:
    """phi = max(1, median(r^2) / 0.4549). About 1 when sites differ only by chance."""
    if not residuals:
        return 1.0
    return max(1.0, median([r * r for r in residuals]) / CHI2_1_MEDIAN)


@dataclass(frozen=True, slots=True)
class ExpectedCount:
    """A site's observed count, its leave-one-out expected count and its units.

    units is what the reference rate for phi is taken per: exposure for a ratio
    expectation (E = rate of the other sites x units), or E itself for a model-based one.
    """

    site: str
    observed: int
    expected: float
    units: float
    rate_others: float


def leave_one_out(
    counts: Mapping[str, int], units: Mapping[str, float], tested: list[str]
) -> list[ExpectedCount]:
    """E_i = (sum of other sites' counts / their units) * units_i, over all other sites.

    Sites with no units, with no other units, or whose expected count is 0 are left out.
    """
    total_count = sum(counts.get(s, 0) for s in units)
    total_units = sum(units.values())
    out: list[ExpectedCount] = []
    for site in tested:
        own_count, own_units = counts.get(site, 0), units.get(site, 0.0)
        other = total_units - own_units
        if own_units <= 0 or other <= 0:
            continue
        rate = (total_count - own_count) / other
        expected = rate * own_units
        if expected <= 0:
            continue
        out.append(ExpectedCount(site, own_count, expected, own_units, rate))
    return out


def quasi_poisson_test(rows: list[ExpectedCount], *, lower_tail: bool) -> QuasiPoissonResult:
    """z_i = (O_i - E_i) / sqrt(phi * E_i); lower_tail: p = Phi(z), else p = 1 - Phi(z).

    phi is estimated from residuals against one robust reference rate m, the median of
    O_i / units_i across the sites: r_i = (O_i - m units_i) / sqrt(m units_i), then
    phi = max(1, median(r_i^2) / 0.4549). Residuals against the leave-one-out E_i would
    not be robust: one outlier site enters every other site's E_i and shifts all their
    residuals at once (a design change from the step 4 spec, agreed 2026-11-07).
    """
    reference = median([r.observed / r.units for r in rows]) if rows else 0.0
    if reference > 0:
        phi = dispersion(
            [(r.observed - reference * r.units) / math.sqrt(reference * r.units) for r in rows]
        )
    else:
        phi = 1.0
    sites: list[QuasiPoissonSite] = []
    for r in rows:
        residual = (r.observed - r.expected) / math.sqrt(r.expected)
        z = (r.observed - r.expected) / math.sqrt(phi * r.expected)
        p = float(stats.norm.cdf(z) if lower_tail else stats.norm.sf(z))
        sites.append(
            QuasiPoissonSite(r.site, r.observed, r.expected, r.rate_others, residual, z, p)
        )
    return QuasiPoissonResult(phi=phi, reference_rate=reference, sites=tuple(sites))


def quasi_poisson(
    counts: Mapping[str, int],
    exposure: Mapping[str, float],
    tested: list[str],
    *,
    lower_tail: bool,
) -> QuasiPoissonResult:
    """Leave-one-out expected counts over exposure, then the quasi-Poisson test."""
    return quasi_poisson_test(leave_one_out(counts, exposure, tested), lower_tail=lower_tail)


def neg_log10(p: float, floor: float = 1e-300) -> float:
    return -math.log10(max(p, floor))
