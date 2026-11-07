"""rbqm.csm: central statistical monitoring of vital signs.

Two tests per site and parameter, both against all other sites pooled:
* variance: one-sided Brown-Forsythe (Levene, center=median); fabricated values cluster.
* terminal digit: chi-square of last digits against the other sites' digit mix.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import polars as pl
from numpy.typing import NDArray
from pydantic import Field
from scipy import stats

from meridian.agents.base import AgentConfig, AgentContext, FindingDraft, make_dedup_key
from meridian.agents.rbqm.common import RbqmAgentBase, cut_id_of, neg_log10, sites_with_min_treated
from meridian.data.derived import treated_subjects
from meridian.domain.enums import FindingKind, Severity
from meridian.domain.events import Event
from meridian.domain.findings import Evidence

CHECKS: tuple[str, ...] = ("variance", "terminal_digit")
# Per parameter p (sysbp, diabp, pulse). p values are Bonferroni-adjusted.
STATISTICS: tuple[str, ...] = (
    "<param>:sd_ratio",
    "<param>:brown_forsythe_p",
    "<param>:terminal_digit_chi2_p",
    "<param>:terminal_zero_share",
)
TESTS_PER_PARAMETER = 2
DIGIT_SMOOTHING = 0.5


class CsmConfig(AgentConfig):
    alpha: float = Field(0.01, gt=0, lt=1)
    sd_ratio_max: float = Field(0.5, gt=0)
    min_subjects: int = Field(5, ge=1)
    min_readings: int = Field(50, ge=1)
    parameters: tuple[str, ...] = ("SYSBP", "DIABP", "PULSE")


@dataclass(frozen=True, slots=True)
class _Flag:
    parameter: str
    test: str
    adjusted_p: float
    evidence: tuple[Evidence, ...]


def _digits(values: NDArray[np.float64]) -> NDArray[np.int64]:
    ints = values[values == np.round(values)]
    digits: NDArray[np.int64] = np.abs(ints).astype(np.int64) % 10
    return digits


def variance_test(site: NDArray[np.float64], others: NDArray[np.float64]) -> tuple[float, float]:
    """(sd_ratio, one-sided p). p is halved when the site is less variable, else 1."""
    site_sd = float(np.std(site, ddof=1))
    others_sd = float(np.std(others, ddof=1))
    ratio = site_sd / others_sd if others_sd > 0 else float("inf")
    p = float(stats.levene(site, others, center="median").pvalue)
    return ratio, (p / 2 if site_sd < others_sd else 1.0)


def terminal_digit_test(
    site: NDArray[np.float64], others: NDArray[np.float64]
) -> tuple[float, int, float]:
    """(chi-square p, integer readings at the site, share ending in 0)."""
    site_digits = _digits(site)
    other_digits = _digits(others)
    observed = np.bincount(site_digits, minlength=10).astype(np.float64)
    reference = np.bincount(other_digits, minlength=10).astype(np.float64) + DIGIT_SMOOTHING
    expected = reference / reference.sum() * observed.sum()
    p = float(stats.chisquare(observed, expected).pvalue)
    zero_share = float(observed[0] / observed.sum()) if observed.sum() else 0.0
    return p, len(site_digits), zero_share


class Csm(RbqmAgentBase):
    agent_id = "rbqm.csm"
    version = "1.0.0"
    config_model = CsmConfig

    def run(self, trigger: Event, ctx: AgentContext[CsmConfig]) -> Sequence[FindingDraft]:
        cfg = ctx.config
        study = ctx.study
        cut_id = cut_id_of(trigger, study)
        dm = study.table("dm")
        tested, skipped = sites_with_min_treated(dm, cfg.min_subjects)
        if skipped:
            ctx.log.debug("csm.sites_skipped", sites=skipped, min_subjects=cfg.min_subjects)
        treated = treated_subjects(dm).select("USUBJID", "SITEID")
        vs = (
            study.table("vs")
            .filter(pl.col("VSTESTCD").is_in(cfg.parameters) & pl.col("VSSTRESN").is_not_null())
            .join(treated, on="USUBJID", how="inner")
            .select("SITEID", "VSTESTCD", "VSSTRESN")
        )
        m = len(tested) * len(cfg.parameters) * TESTS_PER_PARAMETER

        drafts: list[FindingDraft] = []
        for site in tested:
            flags: list[_Flag] = []
            for parameter in cfg.parameters:
                rows = vs.filter(pl.col("VSTESTCD") == parameter)
                own = rows.filter(pl.col("SITEID") == site)["VSSTRESN"].to_numpy()
                rest = rows.filter(pl.col("SITEID") != site)["VSSTRESN"].to_numpy()
                if len(own) < 2 or len(rest) < 2:
                    continue
                name = parameter.lower()
                selector = f"SITEID={site};VSTESTCD={parameter}"
                ratio, p_one = variance_test(own, rest)
                adj_var = min(1.0, p_one * m)
                if ratio < cfg.sd_ratio_max and adj_var < cfg.alpha:
                    flags.append(
                        _Flag(
                            parameter,
                            "variance",
                            adj_var,
                            (
                                Evidence(
                                    source="blinded/vs",
                                    selector=selector,
                                    statistic=f"{name}:sd_ratio",
                                    value=ratio,
                                    threshold=cfg.sd_ratio_max,
                                ),
                                Evidence(
                                    source="blinded/vs",
                                    selector=selector,
                                    statistic=f"{name}:brown_forsythe_p",
                                    value=adj_var,
                                    threshold=cfg.alpha,
                                ),
                            ),
                        )
                    )
                p_digit, n_ints, zero_share = terminal_digit_test(own, rest)
                if n_ints >= cfg.min_readings:
                    adj_digit = min(1.0, p_digit * m)
                    if adj_digit < cfg.alpha:
                        flags.append(
                            _Flag(
                                parameter,
                                "terminal_digit",
                                adj_digit,
                                (
                                    Evidence(
                                        source="blinded/vs",
                                        selector=selector,
                                        statistic=f"{name}:terminal_digit_chi2_p",
                                        value=adj_digit,
                                        threshold=cfg.alpha,
                                    ),
                                    Evidence(
                                        source="blinded/vs",
                                        selector=selector,
                                        statistic=f"{name}:terminal_zero_share",
                                        value=zero_share,
                                        threshold=None,
                                    ),
                                ),
                            )
                        )
            if not flags:
                continue
            by_param: dict[str, set[str]] = {}
            for flag in flags:
                by_param.setdefault(flag.parameter, set()).add(flag.test)
            both = any(len(tests) == TESTS_PER_PARAMETER for tests in by_param.values())
            described = "; ".join(
                f"{p}: {' and '.join(sorted(t))}" for p, t in sorted(by_param.items())
            )
            drafts.append(
                FindingDraft(
                    dedup_key=make_dedup_key(
                        agent_id=self.agent_id,
                        agent_version=self.version,
                        kind=FindingKind.SITE_DATA_ANOMALY,
                        study_id=study.study_id,
                        site_id=site,
                        cut_id=cut_id,
                        check=None,
                    ),
                    kind=FindingKind.SITE_DATA_ANOMALY,
                    site_id=site,
                    severity=Severity.HIGH if both else Severity.MEDIUM,
                    title=f"Site {site}: vital signs look statistically unusual",
                    summary=(
                        f"Site {site} vital signs differ from the other sites ({described}). "
                        f"p values are Bonferroni-adjusted over {m} tests."
                    ),
                    evidence=tuple(e for flag in flags for e in flag.evidence),
                    score=max(neg_log10(flag.adjusted_p) for flag in flags),
                )
            )
        return drafts
