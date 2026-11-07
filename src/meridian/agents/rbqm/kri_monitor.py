"""rbqm.kri_monitor: key risk indicators per site."""

import math
from collections.abc import Sequence

import polars as pl
from pydantic import Field

from meridian.agents.base import AgentConfig, AgentContext, FindingDraft, make_dedup_key
from meridian.agents.rbqm.common import (
    RbqmAgentBase,
    cut_id_of,
    exposure,
    loo_poisson,
    neg_log10,
    sites_with_min_treated,
    treated_ids,
)
from meridian.domain.enums import FindingKind, Severity
from meridian.domain.events import Event
from meridian.domain.findings import Evidence

# Check names, used as the metric part of the dedup key and as evidence prefixes.
CHECKS: tuple[str, ...] = ("ae_rate", "deviations", "data_entry_lag", "query_aging")
# Evidence statistics per check. p values called *_adjusted are Bonferroni-adjusted.
STATISTICS: dict[str, tuple[str, ...]] = {
    "ae_rate": (
        "ae_rate:observed",
        "ae_rate:expected",
        "ae_rate:per_100_subject_days",
        "ae_rate:poisson_p",
        "ae_rate:poisson_p_adjusted",
        "ae_rate:excluded_subjects",
    ),
    "deviations": (
        "deviations:observed",
        "deviations:expected",
        "deviations:poisson_p",
        "deviations:poisson_p_adjusted",
        "deviations:excluded_subjects",
    ),
    "data_entry_lag": ("data_entry_lag:median_days", "data_entry_lag:entries"),
    "query_aging": ("query_aging:median_open_age_days", "query_aging:open_queries"),
}


class KriMonitorConfig(AgentConfig):
    alpha: float = Field(0.01, gt=0, lt=1)
    lag_days: float = Field(7.0, gt=0)
    query_age_days: float = Field(21.0, gt=0)
    min_subjects: int = Field(5, ge=1)
    min_open_queries: int = Field(5, ge=1)


def _median_severity(median: float, threshold: float) -> Severity:
    return Severity.HIGH if median > 1.5 * threshold else Severity.MEDIUM


class KriMonitor(RbqmAgentBase):
    agent_id = "rbqm.kri_monitor"
    version = "1.0.0"
    config_model = KriMonitorConfig

    def run(self, trigger: Event, ctx: AgentContext[KriMonitorConfig]) -> Sequence[FindingDraft]:
        cfg = ctx.config
        study = ctx.study
        cut_id = cut_id_of(trigger, study)
        dm, sv = study.table("dm"), study.table("sv")
        treated = treated_ids(dm)
        exp = exposure(dm, sv)
        included = exp.per_subject["USUBJID"].to_list()
        days = dict(zip(exp.frame["SITEID"], exp.frame["subject_days"], strict=True))
        tested, skipped = sites_with_min_treated(dm, cfg.min_subjects)
        if skipped:
            ctx.log.debug("kri_monitor.sites_skipped", sites=skipped, min_subjects=cfg.min_subjects)

        def key(kind: FindingKind, site: str, check: str) -> str:
            return make_dedup_key(
                agent_id=self.agent_id,
                kind=kind,
                study_id=study.study_id,
                site_id=site,
                cut_id=cut_id,
                metric=check,
            )

        drafts: list[FindingDraft] = []

        # ae_rate and deviations: leave-one-out Poisson, Bonferroni over sites tested.
        site_of = exp.per_subject.select("USUBJID", "SITEID")
        ae_counts = dict(
            study.table("ae")
            .filter(pl.col("USUBJID").is_in(included))
            .join(site_of, on="USUBJID")
            .group_by("SITEID")
            .len()
            .iter_rows()
        )
        dev_counts = dict(
            study.table("deviations")
            .filter(pl.col("USUBJID").is_in(included))
            .group_by("SITEID")
            .len()
            .iter_rows()
        )
        for check, counts, lower, kind in (
            ("ae_rate", ae_counts, True, FindingKind.UNDER_REPORTING),
            ("deviations", dev_counts, False, FindingKind.KRI_BREACH),
        ):
            n_tested = len(tested)
            for res in loo_poisson(counts, days, tested, lower_tail=lower):
                adjusted = min(1.0, res.p * n_tested)
                if adjusted >= cfg.alpha:
                    continue
                site_exp = exp.row(res.site)
                selector = f"SITEID={res.site}"
                source = "blinded/ae" if check == "ae_rate" else "ops/deviations"
                evidence = [
                    Evidence(
                        source=source,
                        selector=selector,
                        statistic=f"{check}:observed",
                        value=res.observed,
                        threshold=None,
                    ),
                    Evidence(
                        source=source,
                        selector=selector,
                        statistic=f"{check}:expected",
                        value=res.expected,
                        threshold=None,
                    ),
                ]
                if check == "ae_rate":
                    evidence.append(
                        Evidence(
                            source=source,
                            selector=selector,
                            statistic="ae_rate:per_100_subject_days",
                            value=100 * res.observed / site_exp["subject_days"],
                            threshold=100 * res.rate_others,
                        )
                    )
                evidence += [
                    Evidence(
                        source=source,
                        selector=selector,
                        statistic=f"{check}:poisson_p",
                        value=res.p,
                        threshold=None,
                    ),
                    Evidence(
                        source=source,
                        selector=selector,
                        statistic=f"{check}:poisson_p_adjusted",
                        value=adjusted,
                        threshold=cfg.alpha,
                    ),
                    Evidence(
                        source="blinded/dm+blinded/sv",
                        selector=selector,
                        statistic=f"{check}:excluded_subjects",
                        value=site_exp["excluded"],
                        threshold=None,
                    ),
                ]
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
                            f"implies {res.expected:.1f}. Poisson p = {res.p:.2e}, "
                            f"{adjusted:.2e} after Bonferroni over {n_tested} sites."
                        ),
                        evidence=tuple(evidence),
                        score=neg_log10(adjusted),
                    )
                )

        # data_entry_lag: site median over treated subjects' visits.
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

        # query_aging: site median age of open queries, if there are enough of them.
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
            drafts,
            key=lambda d: (order[d.dedup_key.rsplit("|", 1)[1]], d.site_id or ""),
        )


assert math.isfinite(neg_log10(0.0))
