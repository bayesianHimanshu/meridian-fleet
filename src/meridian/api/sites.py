"""Per-site RBQM metrics for one cut, from the same meridian.stats functions the agents use.

Read on the host from the cut's blinded and ops tables (ParquetStudyReader never opens
the unblinded area). The values match what kri_monitor and csm compute with the configured
parameters; flagged checks come from the caller's visible findings (Repository).
"""

from collections.abc import Mapping, Sequence

from meridian.api.models import QtlView, RbqmSites, SiteMetrics
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


def site_metrics(
    study: StudyReader, agents: AgentsSettings, flagged: Mapping[str, Sequence[str]]
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
    ae_z = {s.site: s.z for s in quasi_poisson_test(ae_rows, lower_tail=True).sites}
    included = exp.per_subject.select("USUBJID", "SITEID")
    deviations = counts_by_site(study.table("deviations"), included)
    dev_z = {
        s.site: s.z for s in quasi_poisson(deviations, exp.days(), tested, lower_tail=False).sites
    }
    lag = {r.site: r.value for r in entry_lag(dm, study.table("data_entry"), kri.lag_days)}
    aging = {
        r.site: r.value
        for r in query_aging(dm, study.table("queries"), kri.query_age_days, kri.min_open_queries)
    }
    csm_tested, _ = sites_with_min_treated(dm, csm.min_subjects)
    vitals = vital_stats(dm, study.table("vs"), csm_tested, csm.parameters, csm.min_readings)
    sd_z = {v.parameter: {s: st.sd_z for s, st in v.sites.items()} for v in vitals}
    treated = {
        str(s): int(n) for s, n in treated_per_site(dm).select("SITEID", "treated").iter_rows()
    }
    sites = []
    for site in sorted(treated):
        sites.append(
            SiteMetrics(
                site_id=site,
                treated=treated[site],
                tested=site in tested,
                ae_rate_z=ae_z.get(site),
                deviation_z=dev_z.get(site),
                median_entry_lag_days=lag.get(site),
                median_open_query_age_days=aging.get(site),
                csm_log_sd_z={p: sd_z.get(p, {}).get(site) for p in csm.parameters},
                flagged_checks=list(flagged.get(site, [])),
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
        thresholds={
            "data_entry_lag_days": kri.lag_days,
            "open_query_age_days": kri.query_age_days,
            "csm_log_sd_z": -csm.z_threshold,
            "kri_alpha": kri.alpha,
        },
    )
