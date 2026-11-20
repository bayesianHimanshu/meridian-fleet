"""Poisson regression with a log link, fitted by iteratively reweighted least squares.

log E[y] = X b + offset. Each step solves the weighted least squares problem
(X' W X) b = X' W z with W = mu and working response z = eta - offset + (y - mu) / mu.
A fit that does not converge (for example a cell with no events, where the MLE does not
exist) raises PoissonFitError rather than returning a half-fitted model.
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]


class PoissonFitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PoissonFit:
    coef: Array
    iterations: int

    def predict(self, x: Array, offset: Array | None = None) -> Array:
        eta = x @ self.coef + (0.0 if offset is None else offset)
        return np.asarray(np.exp(eta), dtype=np.float64)


def fit_poisson(
    x: Array, y: Array, offset: Array | None = None, *, max_iter: int = 100, tol: float = 1e-10
) -> PoissonFit:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n, k = x.shape
    if y.shape != (n,) or np.any(y < 0):
        raise PoissonFitError("y must be a vector of non-negative counts, one per row of x")
    off = np.zeros(n) if offset is None else np.asarray(offset, dtype=np.float64)
    if np.linalg.matrix_rank(x) < k:
        raise PoissonFitError("design matrix is rank deficient")
    eta = np.log(y + 0.5)  # start from the data, as in the usual IRLS initialisation
    coef = np.zeros(k)
    for iteration in range(1, max_iter + 1):
        mu = np.exp(eta)
        z = eta - off + (y - mu) / mu
        xw = x * mu[:, None]
        try:
            new = np.linalg.solve(x.T @ xw, xw.T @ z)
        except np.linalg.LinAlgError as exc:
            raise PoissonFitError("IRLS diverged; the MLE may not exist") from exc
        if not np.all(np.isfinite(new)):
            raise PoissonFitError("IRLS diverged; the MLE may not exist")
        eta = x @ new + off
        if iteration > 1 and np.max(np.abs(new - coef)) <= tol * (1.0 + np.max(np.abs(new))):
            return PoissonFit(coef=new, iterations=iteration)
        coef = new
    raise PoissonFitError(f"IRLS did not converge in {max_iter} iterations; the MLE may not exist")
