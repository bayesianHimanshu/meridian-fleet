"""Settings loaded from config/default.toml, overridable by MERIDIAN_ environment variables.

Precedence, highest first: constructor arguments, environment variables, the TOML file,
field defaults. The TOML file path defaults to config/default.toml relative to the
working directory and can be changed with MERIDIAN_CONFIG_FILE. Nested values use a
double underscore in environment variables, for example MERIDIAN_OPS_SYNTH__ENTRY_LAG_SIGMA.
"""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from meridian.data.ops_synth import OpsSynthParams
from meridian.platform.agent_settings import CsmConfig, KriMonitorConfig, QtlDetectorConfig

CONFIG_FILE_ENV = "MERIDIAN_CONFIG_FILE"
DEFAULT_CONFIG_FILE = Path("config/default.toml")

DEFAULT_FINDING_ID_PREFIXES: dict[str, str] = {
    "rbqm": "RBQM",
    "safety": "SAF",
    "quality": "QA",
    "preclinical": "PRE",
    "tmf": "TMF",
    "unblinded": "DSMB",
}

PILOT_BASE_URL = (
    "https://github.com/cdisc-org/sdtm-adam-pilot-project/raw/master/"
    "updated-pilot-submission-package/900172/m5/datasets/cdiscpilot01/tabulations/sdtm"
)


def config_file_path() -> Path:
    return Path(os.environ.get(CONFIG_FILE_ENV, str(DEFAULT_CONFIG_FILE)))


class PilotSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = PILOT_BASE_URL
    domains: tuple[str, ...] = ("dm", "ae", "vs", "lb", "ds", "sv", "ex")
    timeout_seconds: float = 120.0


class AgentsSettings(BaseModel):
    """Which agents run, and each agent's own configuration ([agents.<name>] in TOML)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: tuple[str, ...] = ("rbqm.kri_monitor", "rbqm.qtl_detector", "rbqm.csm")
    kri_monitor: KriMonitorConfig = Field(default_factory=KriMonitorConfig)
    qtl_detector: QtlDetectorConfig
    csm: CsmConfig = Field(default_factory=CsmConfig)


class RuntimeSettings(BaseModel):
    """Where agents run ([runtime] in TOML)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executor: Literal["inprocess", "openshell"] = "inprocess"
    # The agent image; empty means meridian-agent:<package version>.
    image: str = ""
    exec_timeout_seconds: int = Field(120, ge=1)
    # Keep a failed sandbox (and log its name) instead of deleting it.
    keep_failed: bool = False
    policies_dir: Path = Path("policies")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MERIDIAN_", env_nested_delimiter="__", extra="ignore", frozen=True
    )

    ledger_path: Path = Path("var/ledger.sqlite3")
    default_seed: int = 0
    finding_id_prefixes: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_FINDING_ID_PREFIXES)
    )
    study_id: str = "MRD-201"
    raw_dir: Path = Path("data/raw/cdisc_pilot")
    curated_root: Path = Path("data/curated")
    sites_file: Path = Path("config/sites.toml")
    ground_truth_dir: Path = Path("data/ground_truth")
    evals_dir: Path = Path("data/evals")
    pilot: PilotSource = Field(default_factory=PilotSource)
    ops_synth: OpsSynthParams = Field(default_factory=OpsSynthParams)
    agents: AgentsSettings
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_file_path()),
        )
