"""Configuration models of the agents. Kept outside meridian.agents so that settings and
the eval harness can read thresholds without importing any agent code."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.domain.enums import Role


class AgentConfig(BaseModel):
    """Base for each agent's own configuration model."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class KriMonitorConfig(AgentConfig):
    alpha: float = Field(0.01, gt=0, lt=1)
    lag_days: float = Field(7.0, gt=0)
    query_age_days: float = Field(21.0, gt=0)
    min_subjects: int = Field(5, ge=1)
    min_open_queries: int = Field(5, ge=1)
    # Expected-AE variant (meridian.stats.ae_expectation): a, b, c, d or e.
    ae_variant: Literal["a", "b", "c", "d", "e"] = "a"
    early_window_days: int = Field(42, ge=1)


class QtlDetectorConfig(AgentConfig):
    discontinuation_limit: float = Field(gt=0, le=1)
    approach_band: float = Field(0.02, ge=0, lt=1)


class CsmConfig(AgentConfig):
    z_threshold: float = Field(3.5, gt=0)
    sd_ratio_max: float = Field(0.6, gt=0)
    min_subjects: int = Field(5, ge=1)
    min_readings: int = Field(50, ge=1)
    parameters: tuple[str, ...] = ("SYSBP", "DIABP", "PULSE")


class SaeReconcilerConfig(AgentConfig):
    """safety.sae_reconciler: the matching rule, the reporting window, and who may see
    which finding. Visibility lives here, next to the agent, not in the API."""

    # Onsets more than this many days apart are an onset mismatch.
    onset_tolerance_days: int = Field(1, ge=0)
    # Fallback match: same subject and preferred term, onsets at most this far apart.
    fallback_window_days: int = Field(3, ge=0)
    # A case received more than this many hours after site awareness is late.
    late_after_hours: float = Field(24.0, gt=0)
    # A site with at least this many late cases gets one site-level finding.
    site_late_min: int = Field(2, ge=1)
    # Subject-level findings carry the subject id; the site-level finding does not.
    subject_visible_to: tuple[Role, ...] = (
        Role.CLINICAL_OPS_LEAD,
        Role.MEDICAL_MONITOR,
        Role.QA_AUDITOR,
    )
    site_visible_to: tuple[Role, ...] = (
        Role.CLINICAL_OPS_LEAD,
        Role.MEDICAL_MONITOR,
        Role.QA_AUDITOR,
        Role.PORTFOLIO_LEAD,
    )

    @model_validator(mode="after")
    def _windows(self) -> SaeReconcilerConfig:
        if self.fallback_window_days < self.onset_tolerance_days:
            raise ValueError("fallback_window_days must be at least onset_tolerance_days")
        if not set(self.subject_visible_to) <= set(self.site_visible_to):
            raise ValueError("subject-level findings may not reach more roles than site-level")
        return self
