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


def quasi_poisson(
    counts: Mapping[str, int],
    exposure: Mapping[str, float],
    tested: list[str],
    *,
    lower_tail: bool,
) -> QuasiPoissonResult:
    """Leave-one-out expected counts, a robust dispersion, and one-sided normal p values.

    E_i = (sum of other sites' counts / their exposure) * exposure_i, over all other sites.
    z_i = (O_i - E_i) / sqrt(phi * E_i). lower_tail: p = Phi(z), else p = 1 - Phi(z).

    phi is estimated from residuals against one robust reference rate m, the median site
    rate across the tested sites: r_i = (O_i - m d_i) / sqrt(m d_i), then
    phi = max(1, median(r_i^2) / 0.4549). Residuals against the leave-one-out E_i would
    not be robust: one outlier site enters every other site's E_i and shifts all their
    residuals at once (a design change from the step 4 spec, agreed 2026-11-07).
    Sites with no exposure, or whose expected count is 0, are left out.
    """
    total_count = sum(counts.get(s, 0) for s in exposure)
    total_exposure = sum(exposure.values())
    base: list[tuple[str, int, float, float, float]] = []
    for site in tested:
        own_count, own_exposure = counts.get(site, 0), exposure.get(site, 0.0)
        other = total_exposure - own_exposure
        if own_exposure <= 0 or other <= 0:
            continue
        rate = (total_count - own_count) / other
        expected = rate * own_exposure
        if expected <= 0:
            continue
        base.append((site, own_count, expected, rate, (own_count - expected) / math.sqrt(expected)))

    reference = median([counts.get(b[0], 0) / exposure[b[0]] for b in base]) if base else 0.0
    if reference > 0:
        phi = dispersion(
            [
                (counts.get(site, 0) - reference * exposure[site])
                / math.sqrt(reference * exposure[site])
                for site, *_ in base
            ]
        )
    else:
        phi = 1.0
    sites: list[QuasiPoissonSite] = []
    for site, observed, expected, rate, residual in base:
        z = (observed - expected) / math.sqrt(phi * expected)
        p = float(stats.norm.cdf(z) if lower_tail else stats.norm.sf(z))
        sites.append(QuasiPoissonSite(site, observed, expected, rate, residual, z, p))
    return QuasiPoissonResult(phi=phi, reference_rate=reference, sites=tuple(sites))


def neg_log10(p: float, floor: float = 1e-300) -> float:
    return -math.log10(max(p, floor))
