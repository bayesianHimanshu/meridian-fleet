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
    # Background sandbox deletion: thread pool size; 0 deletes synchronously (step 1).
    delete_workers: int = Field(4, ge=0, le=32)
    # A deleted sandbox must leave `openshell sandbox list` within this many seconds.
    delete_confirm_seconds: float = Field(60.0, gt=0)
    # Append-only record of every sandbox this code creates and its teardown.
    sandboxes_log: Path = Path("var/sandboxes.jsonl")
    # meridian sandbox conformance: how long to poll openshell logs for the denial line.
    conformance_log_timeout_seconds: float = Field(30.0, gt=0)


class LlmSettings(BaseModel):
    """The model the narrator calls ([llm] in TOML)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = "claude-haiku-5-5"
    max_tokens: int = Field(300, ge=1)
    # None leaves temperature out of the request. claude-haiku-5-5 refuses it: HTTP 400
    # "`temperature` is deprecated for this model." (measured 2026-11-07).
    temperature: float | None = None
    disable_thinking: bool = True
    timeout_seconds: float = Field(30.0, gt=0)


class NarratorSettings(BaseModel):
    """rbqm.narrator ([narrator] in TOML)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Model calls per runner pass; findings over budget wait for the next pass.
    max_calls_per_pass: int = Field(20, ge=1)


class ApiSettings(BaseModel):
    """The Fleet API ([api] in TOML). It always binds to 127.0.0.1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: int = Field(8765, ge=1024, le=65535)
    people_file: Path = Path("config/people.toml")
    portfolio_file: Path = Path("config/portfolio.toml")
    openapi_file: Path = Path("openapi/meridian-api.json")


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
    llm: LlmSettings = Field(default_factory=LlmSettings)
    narrator: NarratorSettings = Field(default_factory=NarratorSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

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
