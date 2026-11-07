"""rbqm.kri_monitor v2: key risk indicators per site.

ae_rate and deviations use a quasi-Poisson test: leave-one-out expected counts, a robust
dispersion phi estimated across the tested sites, and z = (O - E) / sqrt(phi * E). Real
sites differ more than a Poisson model allows; phi absorbs that. data_entry_lag and
query_aging compare site medians with absolute operational thresholds.
"""

from collections.abc import Sequence
from functools import partial

import polars as pl

from meridian.agents.base import AgentContext, FindingDraft, make_dedup_key, parse_dedup_key
from meridian.agents.rbqm.common import RbqmAgentBase, cut_id_of
from meridian.data.derived import treated_subjects
from meridian.domain.enums import FindingKind, Severity
from meridian.domain.events import Event
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import KriMonitorConfig
from meridian.stats.robust import neg_log10, quasi_poisson
from meridian.stats.site_metrics import counts_by_site, exposure, sites_with_min_treated

__all__ = ["CHECKS", "STATISTICS", "KriMonitor", "KriMonitorConfig"]

# Check names: the last part of the dedup key and the prefix of every evidence statistic.
CHECKS: tuple[str, ...] = ("ae_rate", "deviations", "data_entry_lag", "query_aging")
_RATE_STATS = ("observed", "expected", "phi", "z", "p", "p_adjusted", "excluded_subjects")
STATISTICS: dict[str, tuple[str, ...]] = {
    "ae_rate": (*(f"ae_rate:{s}" for s in _RATE_STATS), "ae_rate:per_100_subject_days"),
    "deviations": tuple(f"deviations:{s}" for s in _RATE_STATS),
    "data_entry_lag": ("data_entry_lag:median_days", "data_entry_lag:entries"),
    "query_aging": ("query_aging:median_open_age_days", "query_aging:open_queries"),
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


def _median_severity(median: float, threshold: float) -> Severity:
    return Severity.HIGH if median > 1.5 * threshold else Severity.MEDIUM


class KriMonitor(RbqmAgentBase):
    agent_id = "rbqm.kri_monitor"
    config_model = KriMonitorConfig

    def run(self, trigger: Event, ctx: AgentContext[KriMonitorConfig]) -> Sequence[FindingDraft]:
        cfg = ctx.config
        study = ctx.study
        cut_id = cut_id_of(trigger, study)
        dm, sv = study.table("dm"), study.table("sv")
        treated = treated_subjects(dm)["USUBJID"].to_list()
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
        rate_checks = (
            ("ae_rate", study.table("ae"), True, FindingKind.UNDER_REPORTING, "blinded/ae"),
            (
                "deviations",
                study.table("deviations"),
                False,
                FindingKind.KRI_BREACH,
                "ops/deviations",
            ),
        )
        for check, rows, lower, kind, source in rate_checks:
            counts = counts_by_site(rows, included)
            result = quasi_poisson(counts, exp.days(), tested, lower_tail=lower)
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
                if check == "ae_rate":
                    evidence.append(
                        ev(
                            "per_100_subject_days",
                            100 * res.observed / site_exp["subject_days"],
                            100 * res.rate_others,
                        )
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
                            f"implies {res.expected:.1f}. With dispersion phi = {result.phi:.2f}, "
                            f"z = {res.z:.2f}, p = {res.p:.2e}, {adjusted:.2e} after Bonferroni "
                            f"over {n_tested} sites."
                        ),
                        evidence=tuple(evidence),
                        score=neg_log10(adjusted),
                    )
                )

        lag = (
            study.table("data_entry")
            .filter(pl.col("USUBJID").is_in(treated))
            .group_by("SITEID")
            .agg(pl.col("entry_lag_days").median().alias("median"), pl.len().alias("n"))
            .sort("SITEID")
        )
        for site, median, n in lag.iter_rows():
            if median is None or median <= cfg.lag_days:
                continue
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
                    evidence=(
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
                    ),
                    score=median / cfg.lag_days,
                )
            )

        aging = (
            study.table("queries")
            .filter(pl.col("USUBJID").is_in(treated) & (pl.col("status") == "open"))
            .group_by("SITEID")
            .agg(pl.col("age_days").median().alias("median"), pl.len().alias("n"))
            .sort("SITEID")
        )
        for site, median, n in aging.iter_rows():
            if n < cfg.min_open_queries or median is None or median <= cfg.query_age_days:
                continue
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
                    evidence=(
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
                    ),
                    score=median / cfg.query_age_days,
                )
            )

        order = {check: i for i, check in enumerate(CHECKS)}
        return sorted(
            drafts, key=lambda d: (order[parse_dedup_key(d.dedup_key).check], d.site_id or "")
        )
