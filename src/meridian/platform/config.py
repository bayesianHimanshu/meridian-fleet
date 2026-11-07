"""Settings loaded from config/default.toml, overridable by MERIDIAN_ environment variables.

Precedence, highest first: constructor arguments, environment variables, the TOML file,
field defaults. The TOML file path defaults to config/default.toml relative to the
working directory and can be changed with MERIDIAN_CONFIG_FILE.
"""

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

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


def config_file_path() -> Path:
    return Path(os.environ.get(CONFIG_FILE_ENV, str(DEFAULT_CONFIG_FILE)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MERIDIAN_", extra="ignore", frozen=True)

    ledger_path: Path = Path("var/ledger.sqlite3")
    default_seed: int = 0
    finding_id_prefixes: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_FINDING_ID_PREFIXES)
    )

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
