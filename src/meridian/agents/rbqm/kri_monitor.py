"""rbqm.kri_monitor v2.2: key risk indicators per site.

ae_rate and deviations use a quasi-Poisson test: leave-one-out expected counts, a robust
dispersion phi estimated across the tested sites, and z = (O - E) / sqrt(phi * E). Real
sites differ more than a Poisson model allows; phi absorbs that. data_entry_lag and
query_aging compare site medians with absolute operational thresholds.

2.1.0 (step 5): the expected AE count comes from the configured variant in
meridian.stats.ae_expectation; variant a is the 2.0.0 subject-days expectation.

2.2.0 (phase 2 step 4): the same findings, with evidence described (role, direction,
expected, display; meridian.agents.rbqm.describe), the median-to-threshold ratio as its
own supporting item, and p values in the summary as their displays (2 significant digits,
floored at 1e-12).
"""

from collections.abc import Sequence
from functools import partial

from meridian.agents.base import AgentContext, FindingDraft, make_dedup_key, parse_dedup_key
from meridian.agents.rbqm.common import RbqmAgentBase, cut_id_of
from meridian.agents.rbqm.describe import describe, fmt_p, ratio_evidence
from meridian.domain.enums import FindingKind, Severity
from meridian.domain.events import Event
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import KriMonitorConfig
from meridian.stats.ae_expectation import AE_VARIANTS, ae_expectation
from meridian.stats.robust import neg_log10, quasi_poisson, quasi_poisson_test
from meridian.stats.rules import entry_lag, query_aging
from meridian.stats.site_metrics import counts_by_site, exposure, sites_with_min_treated

__all__ = ["CHECKS", "STATISTICS", "KriMonitor", "KriMonitorConfig"]

# Check names: the last part of the dedup key and the prefix of every evidence statistic.
CHECKS: tuple[str, ...] = ("ae_rate", "deviations", "data_entry_lag", "query_aging")
_RATE_STATS = ("observed", "expected", "phi", "z", "p", "p_adjusted", "excluded_subjects")
STATISTICS: dict[str, tuple[str, ...]] = {
    "ae_rate": (*(f"ae_rate:{s}" for s in _RATE_STATS), "ae_rate:per_100_subject_days"),
    "deviations": tuple(f"deviations:{s}" for s in _RATE_STATS),
    "data_entry_lag": (
        "data_entry_lag:median_days",
        "data_entry_lag:entries",
        "data_entry_lag:ratio_to_threshold",
    ),
    "query_aging": (
        "query_aging:median_open_age_days",
        "query_aging:open_queries",
        "query_aging:ratio_to_threshold",
    ),
}


def _evidence(
    source: str, selector: str, check: str, stat: str, value: float, threshold: float | None = None
) -> Evidence:
    return Evidence(
        source=source,
        selector=selector,
        statistic=f"{check}:{stat}",
        value=value,
        threshold=threshold,
    )


# A median above this many times its threshold is high severity, otherwise medium.
HIGH_SEVERITY_FACTOR = 1.5


def _median_severity(median: float, threshold: float) -> Severity:
    return Severity.HIGH if median > HIGH_SEVERITY_FACTOR * threshold else Severity.MEDIUM


class KriMonitor(RbqmAgentBase):
    agent_id = "rbqm.kri_monitor"
    # 2.1.0: the configurable expected-AE variant (phase 1 step 5). 2.2.0: described
    # evidence, the same findings.
    version = "2.2.0"
    config_model = KriMonitorConfig

    def run(self, trigger: Event, ctx: AgentContext[KriMonitorConfig]) -> Sequence[FindingDraft]:
        cfg = ctx.config
        study = ctx.study
        cut_id = cut_id_of(trigger, study)
        dm, sv = study.table("dm"), study.table("sv")
        exp = exposure(dm, sv)
        tested, skipped = sites_with_min_treated(dm, cfg.min_subjects)
        if skipped:
            ctx.log.debug("kri_monitor.sites_skipped", sites=skipped, min_subjects=cfg.min_subjects)

        def key(kind: FindingKind, site: str, check: str) -> str:
            return make_dedup_key(
                agent_id=self.agent_id,
                agent_version=self.version,
                kind=kind,
                study_id=study.study_id,
                site_id=site,
                cut_id=cut_id,
                check=check,
            )

        drafts: list[FindingDraft] = []
        included = exp.per_subject.select("USUBJID", "SITEID")
        ae_rows = ae_expectation(
            cfg.ae_variant,
            dm=dm,
            sv=sv,
            ae=study.table("ae"),
            lb=study.table("lb"),
            tested=tested,
            early_window_days=cfg.early_window_days,
        )
        deviation_counts = counts_by_site(study.table("deviations"), included)
        rate_checks = (
            (
                "ae_rate",
                quasi_poisson_test(ae_rows, lower_tail=True),
                True,
                FindingKind.UNDER_REPORTING,
                "blinded/ae",
            ),
            (
                "deviations",
                quasi_poisson(deviation_counts, exp.days(), tested, lower_tail=False),
                False,
                FindingKind.KRI_BREACH,
                "ops/deviations",
            ),
        )
        for check, result, lower, kind, source in rate_checks:
            n_tested = len(result.sites)
            for res in result.sites:
                adjusted = min(1.0, res.p * n_tested)
                if adjusted >= cfg.alpha:
                    continue
                site_exp = exp.row(res.site)
                sel = f"SITEID={res.site}"

                ev = partial(_evidence, source, sel, check)
                evidence = [
                    ev("observed", res.observed),
                    ev("expected", res.expected),
                    ev("phi", result.phi),
                    ev("z", res.z),
                    ev("p", res.p),
                    ev("p_adjusted", adjusted, cfg.alpha),
                    ev("excluded_subjects", site_exp["excluded"]),
                ]
                if check == "ae_rate" and site_exp["subject_days"] > 0:
                    evidence.append(
                        ev(
                            "per_100_subject_days",
                            100 * res.observed / site_exp["subject_days"],
                            100 * res.expected / site_exp["subject_days"],
                        )
                    )
                basis = (
                    f" (expected-AE variant {cfg.ae_variant}, {AE_VARIANTS[cfg.ae_variant]})"
                    if check == "ae_rate"
                    else ""
                )
                direction = "fewer" if lower else "more"
                noun = "adverse events" if check == "ae_rate" else "protocol deviations"
                drafts.append(
                    FindingDraft(
                        dedup_key=key(kind, res.site, check),
                        kind=kind,
                        site_id=res.site,
                        severity=Severity.HIGH if adjusted < cfg.alpha / 10 else Severity.MEDIUM,
                        title=f"Site {res.site}: {direction} {noun} than expected",
                        summary=(
                            f"Site {res.site} has {res.observed} {noun} over "
                            f"{site_exp['subject_days']} subject-days; the rest of the study "
                            f"implies {res.expected:.1f}{basis}. With dispersion phi = "
                            f"{result.phi:.2f}, "
                            f"z = {res.z:.2f}, p = {fmt_p(res.p)}, {fmt_p(adjusted)} after "
                            f"Bonferroni over {n_tested} sites."
                        ),
                        evidence=describe(evidence),
                        score=neg_log10(adjusted),
                    )
                )

        for lag in entry_lag(dm, study.table("data_entry"), cfg.lag_days):
            if not lag.met or lag.value is None:
                continue
            site, median, n = lag.site, lag.value, lag.n
            drafts.append(
                FindingDraft(
                    dedup_key=key(FindingKind.KRI_BREACH, site, "data_entry_lag"),
                    kind=FindingKind.KRI_BREACH,
                    site_id=site,
                    severity=_median_severity(median, cfg.lag_days),
                    title=f"Site {site}: data entry lag above threshold",
                    summary=(
                        f"Median entry lag at site {site} is {median:.1f} days over {n} visits; "
                        f"the threshold is {cfg.lag_days:g} days."
                    ),
                    evidence=describe(
                        (
                            Evidence(
                                source="ops/data_entry",
                                selector=f"SITEID={site}",
                                statistic="data_entry_lag:median_days",
                                value=median,
                                threshold=cfg.lag_days,
                            ),
                            Evidence(
                                source="ops/data_entry",
                                selector=f"SITEID={site}",
                                statistic="data_entry_lag:entries",
                                value=n,
                                threshold=None,
                            ),
                            ratio_evidence(
                                "ops/data_entry",
                                f"SITEID={site}",
                                "data_entry_lag",
                                median,
                                cfg.lag_days,
                            ),
                        )
                    ),
                    score=median / cfg.lag_days,
                )
            )

        aging = query_aging(dm, study.table("queries"), cfg.query_age_days, cfg.min_open_queries)
        for age in aging:
            if not age.met or age.value is None:
                continue
            site, median, n = age.site, age.value, age.n
            drafts.append(
                FindingDraft(
                    dedup_key=key(FindingKind.KRI_BREACH, site, "query_aging"),
                    kind=FindingKind.KRI_BREACH,
                    site_id=site,
                    severity=_median_severity(median, cfg.query_age_days),
                    title=f"Site {site}: open queries are ageing",
                    summary=(
                        f"Median age of {n} open queries at site {site} is {median:.1f} days; "
                        f"the threshold is {cfg.query_age_days:g} days."
                    ),
                    evidence=describe(
                        (
                            Evidence(
                                source="ops/queries",
                                selector=f"SITEID={site};status=open",
                                statistic="query_aging:median_open_age_days",
                                value=median,
                                threshold=cfg.query_age_days,
                            ),
                            Evidence(
                                source="ops/queries",
                                selector=f"SITEID={site};status=open",
                                statistic="query_aging:open_queries",
                                value=n,
                                threshold=None,
                            ),
                            ratio_evidence(
                                "ops/queries",
                                f"SITEID={site};status=open",
                                "query_aging",
                                median,
                                cfg.query_age_days,
                            ),
                        )
                    ),
                    score=median / cfg.query_age_days,
                )
            )

        order = {check: i for i, check in enumerate(CHECKS)}
        return sorted(
            drafts, key=lambda d: (order[parse_dedup_key(d.dedup_key).check], d.site_id or "")
        )
