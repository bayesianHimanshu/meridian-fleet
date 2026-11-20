import math

import numpy as np
import pytest

from meridian.stats.poisson_glm import PoissonFitError, fit_poisson


def test_hand_checked_two_group_example() -> None:
    """Group 0: 2 and 4 events over 1 day each (rate 3); group 1: 10 over 2 days (rate 5).

    With one indicator the MLE is the group rates: b0 = log 3, b1 = log(5 / 3).
    """
    x = np.array([[1.0, 0.0], [1.0, 0.0], [1.0, 1.0]])
    y = np.array([2.0, 4.0, 10.0])
    offset = np.log(np.array([1.0, 1.0, 2.0]))
    fit = fit_poisson(x, y, offset)
    assert fit.coef == pytest.approx([math.log(3.0), math.log(5.0 / 3.0)], abs=1e-9)
    assert fit.predict(np.array([[1.0, 1.0]]), np.log(np.array([4.0])))[0] == pytest.approx(20.0)


def test_intercept_only_is_the_pooled_rate() -> None:
    y = np.array([0.0, 3.0, 1.0, 7.0])
    t = np.array([2.0, 5.0, 1.0, 12.0])
    fit = fit_poisson(np.ones((4, 1)), y, np.log(t))
    assert fit.coef[0] == pytest.approx(math.log(11.0 / 20.0), abs=1e-10)
    # The fitted total equals the observed total, a property of the Poisson MLE.
    assert fit.predict(np.ones((4, 1)), np.log(t)).sum() == pytest.approx(11.0)


def test_recovers_known_coefficients_from_simulated_data() -> None:
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence(20261110)))
    n = 20_000
    x = np.column_stack([np.ones(n), rng.integers(0, 2, n).astype(float), rng.normal(0.0, 1.0, n)])
    exposure = rng.uniform(30.0, 200.0, n)
    true = np.array([-3.0, 0.4, -0.25])
    y = rng.poisson(np.exp(x @ true) * exposure).astype(float)
    fit = fit_poisson(x, y, np.log(exposure))
    assert fit.coef == pytest.approx(true, abs=0.03)


def test_rank_deficient_design_and_bad_counts_raise() -> None:
    x = np.array([[1.0, 2.0], [1.0, 2.0], [1.0, 2.0]])
    with pytest.raises(PoissonFitError, match="rank"):
        fit_poisson(x, np.array([1.0, 2.0, 3.0]))
    with pytest.raises(PoissonFitError):
        fit_poisson(np.ones((2, 1)), np.array([1.0, -1.0]))


def test_a_cell_with_no_events_and_no_mle_raises() -> None:
    """The only subjects with x = 1 have no events: b1 runs off to minus infinity."""
    x = np.array([[1.0, 0.0], [1.0, 0.0], [1.0, 1.0], [1.0, 1.0]])
    with pytest.raises(PoissonFitError):
        fit_poisson(x, np.array([3.0, 2.0, 0.0, 0.0]), max_iter=500)
