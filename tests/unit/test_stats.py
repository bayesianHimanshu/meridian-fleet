import math

import numpy as np
import pytest
from scipy import stats

from meridian.stats.robust import (
    CHI2_1_MEDIAN,
    MAD_SCALE,
    dispersion,
    quasi_poisson,
    robust_z,
)


def test_chi2_constant_is_the_chi2_1_median() -> None:
    assert stats.chi2.median(1) == pytest.approx(CHI2_1_MEDIAN, abs=1e-4)


def test_robust_z_small_example() -> None:
    z = robust_z({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 100.0})
    assert z is not None
    # median 3, absolute deviations 2, 1, 0, 1, 97, so MAD = 1
    assert z["c"] == 0.0
    assert z["a"] == pytest.approx(-2 / MAD_SCALE)
    assert z["e"] == pytest.approx(97 / MAD_SCALE)


def test_robust_z_mad_zero_is_none() -> None:
    assert robust_z({"a": 1.0, "b": 1.0, "c": 1.0, "d": 5.0}) is None
    assert robust_z({}) is None


def test_quasi_poisson_hand_checked_example() -> None:
    counts = {"A": 5, "B": 10, "C": 10}
    days = {"A": 10.0, "B": 10.0, "C": 10.0}
    result = quasi_poisson(counts, days, ["A", "B", "C"], lower_tail=True)
    a, b, c = result.sites
    # A: others (10 + 10) / 20 = 1 per day, E = 10, r = -5 / sqrt(10)
    assert a.expected == pytest.approx(10.0)
    assert a.residual == pytest.approx(-5 / math.sqrt(10))
    # B and C: others 15 / 20, E = 7.5, r = 2.5 / sqrt(7.5)
    assert b.expected == pytest.approx(7.5)
    assert c.residual == pytest.approx(2.5 / math.sqrt(7.5))
    # phi uses the median site rate m = median(0.5, 1, 1) = 1 as reference:
    # r' = -5/sqrt(10), 0, 0; median r'^2 = 0, so phi = max(1, 0) = 1.
    assert result.reference_rate == pytest.approx(1.0)
    assert result.phi == 1.0
    assert a.z == pytest.approx(-5 / math.sqrt(10))
    assert a.z == pytest.approx(-1.5811, abs=1e-4)
    assert a.p == pytest.approx(stats.norm.cdf(a.z))
    assert a.p == pytest.approx(0.0569, abs=1e-4)
    # With dispersion: four sites at 10, 20, 30, 40 per 10 days, reference m = 2.5.
    wide = quasi_poisson(
        {"A": 10, "B": 20, "C": 30, "D": 40},
        dict.fromkeys("ABCD", 10.0),
        list("ABCD"),
        lower_tail=True,
    )
    # r' = (-15, -5, 5, 15) / 5 = -3, -1, 1, 3; median r'^2 = 5; phi = 5 / 0.4549
    assert wide.phi == pytest.approx(5 / 0.4549)
    a_wide = wide.sites[0]
    assert a_wide.expected == pytest.approx(30.0)  # (20 + 30 + 40) / 30 * 10
    assert a_wide.z == pytest.approx((10 - 30) / math.sqrt(wide.phi * 30))
    upper = quasi_poisson(counts, days, ["A"], lower_tail=False).sites[0]
    assert upper.p == pytest.approx(stats.norm.sf(upper.z))


def test_leave_one_out_excludes_the_site_under_test() -> None:
    counts = {"A": 100, "B": 10, "C": 10}
    days = {"A": 10.0, "B": 10.0, "C": 10.0}
    sites = {s.site: s for s in quasi_poisson(counts, days, ["A", "B"], lower_tail=False).sites}
    assert sites["A"].rate_others == pytest.approx(1.0)
    assert sites["A"].expected == pytest.approx(10.0)
    assert sites["B"].rate_others == pytest.approx(110 / 20)
    assert "C" not in sites


def _sites(counts: np.ndarray) -> tuple[dict[str, int], dict[str, float], list[str]]:
    names = [f"S{i:02d}" for i in range(len(counts))]
    return (
        {n: int(c) for n, c in zip(names, counts, strict=True)},
        dict.fromkeys(names, 100.0),
        names,
    )


def test_phi_is_one_for_poisson_like_data() -> None:
    assert dispersion([0.1, -0.2, 0.3]) == 1.0
    phis = []
    for seed in range(20):
        counts, days, tested = _sites(np.random.default_rng(seed).poisson(100, 30))
        phis.append(quasi_poisson(counts, days, tested, lower_tail=True).phi)
    assert float(np.median(phis)) < 1.3


def test_phi_grows_with_overdispersion() -> None:
    rng = np.random.default_rng(1)
    medians = []
    for shape in (1000.0, 20.0, 4.0):
        phis = []
        for _ in range(10):
            rates = rng.gamma(shape, 100 / shape, 30)
            counts, days, tested = _sites(rng.poisson(rates))
            phis.append(quasi_poisson(counts, days, tested, lower_tail=True).phi)
        medians.append(float(np.median(phis)))
    assert medians[0] < medians[1] < medians[2]
    assert medians[2] > 5


def test_phi_is_robust_to_one_outlier_site() -> None:
    counts_arr = np.random.default_rng(3).poisson(100, 30)
    counts, days, tested = _sites(counts_arr)
    base = quasi_poisson(counts, days, tested, lower_tail=True).phi
    counts["S00"] = 1000
    with_outlier = quasi_poisson(counts, days, tested, lower_tail=False)
    assert with_outlier.phi < 2 * max(base, 1.0)
    outlier = next(s for s in with_outlier.sites if s.site == "S00")
    assert outlier.p < 1e-10
