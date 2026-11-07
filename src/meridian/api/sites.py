"""Per-site RBQM metrics for one cut, from the same meridian.stats functions the agents use.

Read on the host from the cut's blinded and ops tables (ParquetStudyReader never opens
the unblinded area). The values match what kri_monitor and csm compute with the configured
parameters; flagged checks come from the caller's visible findings (Repository).

Each column's band follows what its agent tests, read from the agent's config: the
direction it tests and its threshold. A z column is critical beyond the agent's own
threshold, warning from absolute z 2 up to it, and gets no fill in a direction the agent
does not test. The medians are critical beyond the agent's threshold and warning above
three quarters of it. The bands are computed here, once, so the UI and the tests agree.
"""

from collections.abc import Mapping, Sequence
from typing import Literal

from scipy import stats

from meridian.api.models import Band, ColumnRule, QtlView, RbqmSites, SiteFlag, SiteMetrics
from meridian.data.derived import treated_per_site
from meridian.data.study_store import StudyReader
from meridian.platform.config import AgentsSettings
from meridian.stats.ae_expectation import ae_expectation
from meridian.stats.robust import quasi_poisson, quasi_poisson_test
from meridian.stats.rules import discontinuation, entry_lag, qtl_status, query_aging
from meridian.stats.site_metrics import (
    counts_by_site,
    exposure,
    sites_with_min_treated,
    vital_stats,
)

Z_WARNING_FROM = 2.0  # absolute z where the warning band starts, for every z column
APPROACH_SHARE = 0.75  # a median above this share of its threshold is warning


def band(value: float | None, rule: ColumnRule) -> Band:
    """The band of one value under its column's rule."""
    if value is None:
        return "na"
    if rule.unit == "z":
        if (rule.direction == "low" and value > 0) or (rule.direction == "high" and value < 0):
            return "untested_direction"
        size = abs(value)
        if size > abs(rule.critical_beyond):
            return "critical"
        return "warning" if size >= rule.warning_from else "normal"
    if value > rule.critical_beyond:
        return "critical"
    return "warning" if value > rule.warning_from else "normal"


def _bonferroni_z(alpha: float, n_tested: int, direction: Literal["low", "high"]) -> float:
    """The z at which kri_monitor flags: min(1, p * n) < alpha, with p = Phi(z) (low) or
    1 - Phi(z) (high), is z beyond the normal quantile of alpha / n."""
    q = alpha / max(n_tested, 1)
    return float(stats.norm.ppf(q) if direction == "low" else stats.norm.isf(q))


def column_rules(
    agents: AgentsSettings, n_ae: int, n_dev: int, parameters: Sequence[str]
) -> dict[str, ColumnRule]:
    kri, csm = agents.kri_monitor, agents.csm
    rules = {
        "ae_rate_z": ColumnRule(
            agent="rbqm.kri_monitor",
            unit="z",
            direction="low",
            warning_from=Z_WARNING_FROM,
            critical_beyond=_bonferroni_z(kri.alpha, n_ae, "low"),
            rule=f"fewer adverse events than expected: Bonferroni-adjusted p below {kri.alpha:g} "
            f"over {n_ae} sites",
        ),
        "deviation_z": ColumnRule(
            agent="rbqm.kri_monitor",
            unit="z",
            direction="high",
            warning_from=Z_WARNING_FROM,
            critical_beyond=_bonferroni_z(kri.alpha, n_dev, "high"),
            rule=f"more protocol deviations than expected: Bonferroni-adjusted p below "
            f"{kri.alpha:g} over {n_dev} sites",
        ),
        "median_entry_lag_days": ColumnRule(
            agent="rbqm.kri_monitor",
            unit="days",
            direction="high",
            warning_from=APPROACH_SHARE * kri.lag_days,
            critical_beyond=kri.lag_days,
            rule=f"median entry lag above {kri.lag_days:g} days",
        ),
        "median_open_query_age_days": ColumnRule(
            agent="rbqm.kri_monitor",
            unit="days",
            direction="high",
            warning_from=APPROACH_SHARE * kri.query_age_days,
            critical_beyond=kri.query_age_days,
            rule=f"median open query age above {kri.query_age_days:g} days, with at least "
            f"{kri.min_open_queries} open queries",
        ),
    }
    for p in parameters:
        rules[f"csm_log_sd_z:{p}"] = ColumnRule(
            agent="rbqm.csm",
            unit="z",
            direction="low",
            warning_from=Z_WARNING_FROM,
            critical_beyond=-csm.z_threshold,
            rule=f"{p} spread unusually small: robust z of log SD below -{csm.z_threshold:g} "
            f"and SD below {csm.sd_ratio_max:g} of the median site",
        )
    return rules


def site_metrics(
    study: StudyReader, agents: AgentsSettings, flagged: Mapping[str, Sequence[SiteFlag]]
) -> RbqmSites:
    kri, csm = agents.kri_monitor, agents.csm
    dm, sv = study.table("dm"), study.table("sv")
    exp = exposure(dm, sv)
    tested, _ = sites_with_min_treated(dm, kri.min_subjects)
    ae_rows = ae_expectation(
        kri.ae_variant,
        dm=dm,
        sv=sv,
        ae=study.table("ae"),
        lb=study.table("lb"),
        tested=tested,
        early_window_days=kri.early_window_days,
    )
    ae_result = quasi_poisson_test(ae_rows, lower_tail=True)
    ae_z = {s.site: s.z for s in ae_result.sites}
    included = exp.per_subject.select("USUBJID", "SITEID")
    deviations = counts_by_site(study.table("deviations"), included)
    dev_result = quasi_poisson(deviations, exp.days(), tested, lower_tail=False)
    dev_z = {s.site: s.z for s in dev_result.sites}
    lag = {r.site: r.value for r in entry_lag(dm, study.table("data_entry"), kri.lag_days)}
    aging_rows = query_aging(dm, study.table("queries"), kri.query_age_days, kri.min_open_queries)
    aging = {r.site: r.value for r in aging_rows}
    open_queries = {r.site: r.n for r in aging_rows}
    csm_tested, _ = sites_with_min_treated(dm, csm.min_subjects)
    vitals = vital_stats(dm, study.table("vs"), csm_tested, csm.parameters, csm.min_readings)
    sd_z = {v.parameter: {s: st.sd_z for s, st in v.sites.items()} for v in vitals}
    sd_ratio = {v.parameter: {s: st.sd_ratio for s, st in v.sites.items()} for v in vitals}
    treated = {
        str(s): int(n) for s, n in treated_per_site(dm).select("SITEID", "treated").iter_rows()
    }
    rules = column_rules(agents, len(ae_result.sites), len(dev_result.sites), csm.parameters)
    sites = []
    for site in sorted(treated):
        values: dict[str, float | None] = {
            "ae_rate_z": ae_z.get(site),
            "deviation_z": dev_z.get(site),
            "median_entry_lag_days": lag.get(site),
            "median_open_query_age_days": aging.get(site),
            **{f"csm_log_sd_z:{p}": sd_z.get(p, {}).get(site) for p in csm.parameters},
        }
        sites.append(
            SiteMetrics(
                site_id=site,
                treated=treated[site],
                tested=site in tested,
                ae_rate_z=ae_z.get(site),
                deviation_z=dev_z.get(site),
                median_entry_lag_days=lag.get(site),
                median_open_query_age_days=aging.get(site),
                open_queries=open_queries.get(site),
                csm_log_sd_z={p: sd_z.get(p, {}).get(site) for p in csm.parameters},
                csm_sd_ratio={p: sd_ratio.get(p, {}).get(site) for p in csm.parameters},
                bands={key: band(values[key], rule) for key, rule in rules.items()},
                flagged_checks=sorted({f.check for f in flagged.get(site, [])}),
                flagged_by=list(flagged.get(site, [])),
            )
        )
    qtl_cfg = agents.qtl_detector
    rule = discontinuation(
        dm, study.table("ds"), qtl_cfg.discontinuation_limit, qtl_cfg.approach_band
    )
    qtl = QtlView(
        name=rule.check,
        rate=rule.value,
        limit=qtl_cfg.discontinuation_limit,
        approach_from=rule.threshold,
        status=None
        if rule.value is None
        else qtl_status(rule.value, qtl_cfg.discontinuation_limit, qtl_cfg.approach_band).value,
        discontinued=rule.count,
        treated=rule.n,
    )
    return RbqmSites(
        study_id=study.study_id,
        cut_id=study.cut_id,
        parameters=list(csm.parameters),
        sites=sites,
        qtl=qtl,
        columns=rules,
    )
