"""rbqm.csm v2.1: central statistical monitoring of vital signs, between sites.

Each tested site is one observation. For each parameter, a site's log SD is compared with
the other tested sites through a robust z (median and MAD), so one or two bad sites
cannot hide each other the way a pooled reference does. Terminal-digit preference is only
supporting evidence: it is attached to a finding the variance test raised and never
raises a finding by itself, because digit preference alone is common in manual blood
pressure measurement.

2.1.0 (phase 2 step 4): the same findings, with evidence described: terminal-digit items
are marked supporting, with the 10% chance level as their reference.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from meridian.agents.base import AgentContext, FindingDraft, make_dedup_key
from meridian.agents.rbqm.common import RbqmAgentBase, cut_id_of
from meridian.agents.rbqm.describe import describe
from meridian.domain.enums import FindingKind, Severity
from meridian.domain.events import Event
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import CsmConfig
from meridian.stats.site_metrics import VitalSiteStat, sites_with_min_treated, vital_stats

__all__ = ["CHECKS", "STATISTICS", "Csm", "CsmConfig"]

CHECKS: tuple[str, ...] = ("variance", "terminal_digit")
# Per parameter (sysbp, diabp, pulse); terminal digit statistics are supporting only.
STATISTICS: tuple[str, ...] = (
    "<param>:log_sd_z",
    "<param>:sd_ratio",
    "<param>:terminal_zero_share",
    "<param>:terminal_zero_z",
)


@dataclass(frozen=True, slots=True)
class _Flag:
    parameter: str
    stat: VitalSiteStat


class Csm(RbqmAgentBase):
    agent_id = "rbqm.csm"
    version = "2.1.0"
    config_model = CsmConfig

    def run(self, trigger: Event, ctx: AgentContext[CsmConfig]) -> Sequence[FindingDraft]:
        cfg = ctx.config
        study = ctx.study
        cut_id = cut_id_of(trigger, study)
        dm = study.table("dm")
        tested, skipped = sites_with_min_treated(dm, cfg.min_subjects)
        if skipped:
            ctx.log.debug("csm.sites_skipped", sites=skipped, min_subjects=cfg.min_subjects)
        per_parameter = vital_stats(dm, study.table("vs"), tested, cfg.parameters, cfg.min_readings)

        flags: dict[str, list[_Flag]] = {}
        for stats in per_parameter:
            if stats.sd_mad_zero:
                ctx.log.info("csm.parameter_skipped", parameter=stats.parameter, reason="MAD is 0")
                continue
            for site, stat in stats.sites.items():
                if stat.sd_z is None:
                    continue
                if stat.sd_z < -cfg.z_threshold and stat.sd_ratio < cfg.sd_ratio_max:
                    flags.setdefault(site, []).append(_Flag(stats.parameter, stat))

        drafts: list[FindingDraft] = []
        for site in sorted(flags):
            site_flags = flags[site]
            evidence: list[Evidence] = []
            digit_strong = False
            for flag in site_flags:
                name = flag.parameter.lower()
                sel = f"SITEID={site};VSTESTCD={flag.parameter}"
                stat = flag.stat
                assert stat.sd_z is not None
                evidence += [
                    Evidence(
                        source="blinded/vs",
                        selector=sel,
                        statistic=f"{name}:log_sd_z",
                        value=stat.sd_z,
                        threshold=-cfg.z_threshold,
                    ),
                    Evidence(
                        source="blinded/vs",
                        selector=sel,
                        statistic=f"{name}:sd_ratio",
                        value=stat.sd_ratio,
                        threshold=cfg.sd_ratio_max,
                    ),
                ]
                if stat.zero_share is not None:
                    evidence.append(
                        Evidence(
                            source="blinded/vs",
                            selector=sel,
                            statistic=f"{name}:terminal_zero_share",
                            value=stat.zero_share,
                            threshold=None,
                        )
                    )
                if stat.zero_z is not None:
                    evidence.append(
                        Evidence(
                            source="blinded/vs",
                            selector=sel,
                            statistic=f"{name}:terminal_zero_z",
                            value=stat.zero_z,
                            threshold=cfg.z_threshold,
                        )
                    )
                    digit_strong = digit_strong or stat.zero_z > cfg.z_threshold
            high = len(site_flags) >= 2 or (len(site_flags) == 1 and digit_strong)
            parameters = ", ".join(f.parameter for f in site_flags)
            drafts.append(
                FindingDraft(
                    dedup_key=make_dedup_key(
                        agent_id=self.agent_id,
                        agent_version=self.version,
                        kind=FindingKind.SITE_DATA_ANOMALY,
                        study_id=study.study_id,
                        site_id=site,
                        cut_id=cut_id,
                        check="variance",
                    ),
                    kind=FindingKind.SITE_DATA_ANOMALY,
                    site_id=site,
                    severity=Severity.HIGH if high else Severity.MEDIUM,
                    title=f"Site {site}: vital signs vary unusually little",
                    summary=(
                        f"Site {site} has a much smaller spread than the other sites for "
                        f"{parameters} (robust z of log SD below -{cfg.z_threshold:g} and SD "
                        f"below {cfg.sd_ratio_max:g} of the median site). Terminal digits are "
                        "attached as supporting evidence only."
                    ),
                    evidence=describe(evidence),
                    score=max(-(f.stat.sd_z or 0.0) for f in site_flags),
                )
            )
        return drafts
