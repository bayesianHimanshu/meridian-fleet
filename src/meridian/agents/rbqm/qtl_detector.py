"""rbqm.qtl_detector v2: study-level quality tolerance limits.

Phase 1 covers premature discontinuation only. Approaching is a fixed band below the
limit (limit - approach_band), not a share of it, so a limit set just above the base rate
does not make the clean base "approaching" by construction.
"""

from collections.abc import Sequence
from enum import StrEnum

from meridian.agents.base import AgentContext, FindingDraft, make_dedup_key
from meridian.agents.rbqm.common import RbqmAgentBase, cut_id_of
from meridian.data.derived import premature_discontinuation
from meridian.domain.enums import FindingKind, Severity
from meridian.domain.events import Event
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import QtlDetectorConfig

__all__ = ["CHECKS", "QTL_NAME", "STATISTICS", "QtlDetector", "QtlDetectorConfig", "QtlStatus"]

QTL_NAME = "premature_discontinuation"
CHECKS: tuple[str, ...] = (QTL_NAME,)
STATISTICS: dict[str, tuple[str, ...]] = {
    QTL_NAME: (
        "premature_discontinuation:rate",
        "premature_discontinuation:discontinued",
        "premature_discontinuation:treated",
        "premature_discontinuation:approach_from",
    )
}


class QtlStatus(StrEnum):
    WITHIN = "within"
    APPROACHING = "approaching"
    BREACHED = "breached"


def qtl_status(rate: float, limit: float, approach_band: float) -> QtlStatus:
    if rate >= limit:
        return QtlStatus.BREACHED
    if rate >= limit - approach_band:
        return QtlStatus.APPROACHING
    return QtlStatus.WITHIN


class QtlDetector(RbqmAgentBase):
    agent_id = "rbqm.qtl_detector"
    config_model = QtlDetectorConfig

    def run(self, trigger: Event, ctx: AgentContext[QtlDetectorConfig]) -> Sequence[FindingDraft]:
        cfg = ctx.config
        study = ctx.study
        cut_id = cut_id_of(trigger, study)
        discontinued, treated = premature_discontinuation(study.table("dm"), study.table("ds"))
        if treated == 0:
            return []
        rate = discontinued / treated
        limit = cfg.discontinuation_limit
        status = qtl_status(rate, limit, cfg.approach_band)
        if status is QtlStatus.WITHIN:
            return []
        approach_from = limit - cfg.approach_band
        selector = "treated subjects; DSCAT=DISPOSITION EVENT"
        return [
            FindingDraft(
                dedup_key=make_dedup_key(
                    agent_id=self.agent_id,
                    agent_version=self.version,
                    kind=FindingKind.QTL_BREACH,
                    study_id=study.study_id,
                    site_id=None,
                    cut_id=cut_id,
                    check=QTL_NAME,
                ),
                kind=FindingKind.QTL_BREACH,
                site_id=None,
                severity=Severity.HIGH if status is QtlStatus.BREACHED else Severity.MEDIUM,
                title=f"Premature discontinuation {status.value} its QTL",
                summary=(
                    f"{discontinued} of {treated} treated subjects ({rate:.1%}) discontinued "
                    f"before completion; the QTL is {limit:.1%} and approaching starts at "
                    f"{approach_from:.1%}."
                ),
                evidence=(
                    Evidence(
                        source="blinded/ds",
                        selector=selector,
                        statistic="premature_discontinuation:rate",
                        value=rate,
                        threshold=limit,
                    ),
                    Evidence(
                        source="blinded/ds",
                        selector=selector,
                        statistic="premature_discontinuation:discontinued",
                        value=discontinued,
                        threshold=None,
                    ),
                    Evidence(
                        source="blinded/dm",
                        selector="RFSTDTC not blank",
                        statistic="premature_discontinuation:treated",
                        value=treated,
                        threshold=None,
                    ),
                    Evidence(
                        source="blinded/ds",
                        selector=selector,
                        statistic="premature_discontinuation:approach_from",
                        value=approach_from,
                        threshold=None,
                    ),
                ),
                score=rate / limit,
            )
        ]
