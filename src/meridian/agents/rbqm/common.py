"""Shared helpers for RBQM agents. Blinded data only."""

import math
from collections.abc import Mapping
from dataclasses import dataclass

import polars as pl
from scipy import stats

from meridian.agents.base import STUDY_SCOPE, Zone
from meridian.data.derived import subject_days, treated_per_site, treated_subjects
from meridian.data.study_store import StudyReader
from meridian.domain.enums import EventType, Role, Visibility
from meridian.domain.events import DataCut, Event

RBQM_VISIBLE_TO = frozenset({Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.QA_AUDITOR})
MIN_P = 1e-300


class RbqmAgentBase:
    """Identity shared by the RBQM agents. Subclasses set agent_id, version, config_model."""

    zone = Zone.RBQM
    subscribes_to = frozenset({EventType.DATA_CUT})
    visibility = Visibility.BLINDED
    visible_to = RBQM_VISIBLE_TO


def cut_id_of(trigger: Event, study: StudyReader) -> str:
    if isinstance(trigger.payload, DataCut) and trigger.payload.cut_id != study.cut_id:
        raise ValueError(
            f"trigger is for cut {trigger.payload.cut_id} but the reader is for {study.cut_id}"
        )
    return study.cut_id


def neg_log10(p: float) -> float:
    return -math.log10(max(p, MIN_P))


@dataclass(frozen=True, slots=True)
class Exposure:
    """Per site: treated subjects, subjects with subject-days, excluded, total subject-days."""

    frame: pl.DataFrame  # SITEID, treated, included, excluded, subject_days
    per_subject: pl.DataFrame  # USUBJID, SITEID, subject_days (non-null only)

    def row(self, site: str) -> dict[str, int]:
        found = self.frame.filter(pl.col("SITEID") == site)
        return {k: int(found[k][0]) for k in ("treated", "included", "excluded", "subject_days")}


def exposure(dm: pl.DataFrame, sv: pl.DataFrame) -> Exposure:
    days = subject_days(dm, sv)
    included = days.filter(pl.col("subject_days").is_not_null())
    frame = (
        treated_per_site(dm)
        .join(
            days.group_by("SITEID", maintain_order=True).agg(
                pl.col("subject_days").is_not_null().sum().alias("included"),
                pl.col("subject_days").is_null().sum().alias("excluded"),
                pl.col("subject_days").sum().alias("subject_days"),
            ),
            on="SITEID",
            how="left",
        )
        .with_columns(pl.col("included", "excluded", "subject_days").fill_null(0))
        .sort("SITEID")
    )
    return Exposure(frame=frame, per_subject=included)


@dataclass(frozen=True, slots=True)
class LooPoisson:
    site: str
    observed: int
    expected: float
    rate_others: float
    p: float


def loo_poisson(
    counts: Mapping[str, int],
    days: Mapping[str, int],
    tested: list[str],
    *,
    lower_tail: bool,
) -> list[LooPoisson]:
    """Leave-one-out Poisson test per tested site against the pooled rate of all others.

    lower_tail: p = P(X <= observed) (under-reporting). Otherwise p = P(X >= observed).
    """
    total_count = sum(counts.get(s, 0) for s in days)
    total_days = sum(days.values())
    out: list[LooPoisson] = []
    for site in tested:
        own_count, own_days = counts.get(site, 0), days[site]
        other_days = total_days - own_days
        if other_days <= 0 or own_days <= 0:
            continue
        rate = (total_count - own_count) / other_days
        expected = rate * own_days
        if lower_tail:
            p = float(stats.poisson.cdf(own_count, expected))
        else:
            p = float(stats.poisson.sf(own_count - 1, expected))
        out.append(LooPoisson(site, own_count, expected, rate, p))
    return out


def sites_with_min_treated(dm: pl.DataFrame, min_subjects: int) -> tuple[list[str], list[str]]:
    """(tested, skipped) site ids by treated count, both sorted."""
    sizes = treated_per_site(dm)
    tested = sizes.filter(pl.col("treated") >= min_subjects)["SITEID"].to_list()
    skipped = sizes.filter(pl.col("treated") < min_subjects)["SITEID"].to_list()
    return sorted(tested), sorted(skipped)


def treated_ids(dm: pl.DataFrame) -> list[str]:
    return treated_subjects(dm)["USUBJID"].to_list()


def site_label(site_id: str | None) -> str:
    return site_id or STUDY_SCOPE
