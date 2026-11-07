"""Configuration models of the agents. Kept outside meridian.agents so that settings and
the eval harness can read thresholds without importing any agent code."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
