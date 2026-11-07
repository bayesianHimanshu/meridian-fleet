"""rbqm.qtl_detector: study-level quality tolerance limits.

Phase 1 covers premature discontinuation only. Missed visits needs the planned visit
schedule, which we did not fetch.
"""

from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field

from meridian.agents.base import AgentConfig, AgentContext, FindingDraft, make_dedup_key
from meridian.agents.rbqm.common import RbqmAgentBase, cut_id_of
from meridian.data.derived import premature_discontinuation
from meridian.domain.enums import FindingKind, Severity
from meridian.domain.events import Event
from meridian.domain.findings import Evidence

QTL_NAME = "premature_discontinuation"
CHECKS: tuple[str, ...] = (QTL_NAME,)
STATISTICS: dict[str, tuple[str, ...]] = {
    QTL_NAME: (
        "premature_discontinuation:rate",
        "premature_discontinuation:discontinued",
        "premature_discontinuation:treated",
        "premature_discontinuation:share_of_limit",
    )
}


class QtlStatus(StrEnum):
    WITHIN = "within"
    APPROACHING = "approaching"
    BREACHED = "breached"


class QtlDetectorConfig(AgentConfig):
    discontinuation_limit: float = Field(gt=0, le=1)
    approaching_fraction: float = Field(0.9, gt=0, lt=1)


def qtl_status(rate: float, limit: float, approaching_fraction: float) -> QtlStatus:
    if rate >= limit:
        return QtlStatus.BREACHED
    if rate >= approaching_fraction * limit:
        return QtlStatus.APPROACHING
    return QtlStatus.WITHIN


class QtlDetector(RbqmAgentBase):
    agent_id = "rbqm.qtl_detector"
    version = "1.0.0"
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
        status = qtl_status(rate, limit, cfg.approaching_fraction)
        if status is QtlStatus.WITHIN:
            return []
        selector = "treated subjects; DSCAT=DISPOSITION EVENT"
        return [
            FindingDraft(
                dedup_key=make_dedup_key(
                    agent_id=self.agent_id,
                    kind=FindingKind.QTL_BREACH,
                    study_id=study.study_id,
                    site_id=None,
                    cut_id=cut_id,
                    metric=QTL_NAME,
                ),
                kind=FindingKind.QTL_BREACH,
                site_id=None,
                severity=Severity.HIGH if status is QtlStatus.BREACHED else Severity.MEDIUM,
                title=f"Premature discontinuation {status.value} its QTL",
                summary=(
                    f"{discontinued} of {treated} treated subjects ({rate:.1%}) discontinued "
                    f"before completion; the QTL is {limit:.1%} and {status.value} starts at "
                    f"{(1.0 if status is QtlStatus.BREACHED else cfg.approaching_fraction):.0%} "
                    "of it."
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
                        statistic="premature_discontinuation:share_of_limit",
                        value=rate / limit,
                        threshold=cfg.approaching_fraction,
                    ),
                ),
                score=rate / limit,
            )
        ]
