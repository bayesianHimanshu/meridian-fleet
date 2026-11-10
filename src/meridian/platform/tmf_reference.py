"""The expected TMF artifacts (config/tmf_expected.toml): which records the Trial Master
File must hold, at which level, from which milestone, and how soon after their date.

Kept outside meridian.agents so that the generator, the API and the agent read the same
list without importing agent code. The file sits next to the config file in use (in the
agent image: /opt/meridian/config/).
"""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.domain.tmf import norm_name

CONFIG_FILE_ENV = "MERIDIAN_CONFIG_FILE"
TMF_EXPECTED_FILE = "tmf_expected.toml"

Level = Literal["trial", "site"]
Milestone = Literal[
    "trial_first_subject",
    "site_activation",
    "initiation_visit",
    "routine_visit",
    "close_out_visit",
    "on_issue",
]
# The milestones a visit stands for, and the visit type it is (meridian.domain.tmf).
VISIT_MILESTONES: dict[str, str] = {
    "initiation_visit": "initiation",
    "routine_visit": "routine",
    "close_out_visit": "close_out",
}
_TRIAL_MILESTONES = frozenset({"trial_first_subject", "on_issue"})


class ReferenceSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    publication: str
    url: str
    numbers: str


class TmfRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    qc_within_days: int = Field(ge=0)
    visit_match_days: int = Field(ge=0)


class ExpectedArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    zone: str = Field(min_length=1)
    level: Level
    milestone: Milestone
    file_within_days: int = Field(ge=0)
    dated_before: Literal["site_first_subject"] | None = None
    essential: bool

    @property
    def visit_type(self) -> str | None:
        """The visit type whose visits each expect one of these, if any."""
        return VISIT_MILESTONES.get(self.milestone)

    @model_validator(mode="after")
    def _consistent(self) -> ExpectedArtifact:
        if (self.level == "trial") != (self.milestone in _TRIAL_MILESTONES):
            raise ValueError(f"{self.key}: milestone {self.milestone} needs another level")
        if self.dated_before is not None and self.level != "site":
            raise ValueError(f"{self.key}: dated_before is for site-level artifacts")
        return self


class TmfReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: ReferenceSource
    rules: TmfRules
    artifacts: tuple[ExpectedArtifact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> TmfReference:
        keys = [a.key for a in self.artifacts]
        names = [norm_name(a.name) for a in self.artifacts]
        if len(set(keys)) != len(keys) or len(set(names)) != len(names):
            raise ValueError("artifact keys and names must be unique")
        visits = [a.visit_type for a in self.artifacts if a.visit_type is not None]
        if len(set(visits)) != len(visits):
            raise ValueError("at most one artifact per visit type")
        return self

    def by_name(self) -> dict[str, ExpectedArtifact]:
        """Normalised artifact name to its entry."""
        return {norm_name(a.name): a for a in self.artifacts}

    def by_key(self) -> dict[str, ExpectedArtifact]:
        return {a.key: a for a in self.artifacts}

    def for_visit(self, visit_type: str) -> ExpectedArtifact | None:
        return next((a for a in self.artifacts if a.visit_type == visit_type), None)


def tmf_expected_path() -> Path:
    config = os.environ.get(CONFIG_FILE_ENV)
    base = Path(config).parent if config else Path("config")
    return base / TMF_EXPECTED_FILE


def load_tmf_reference(path: Path | None = None) -> TmfReference:
    with (path or tmf_expected_path()).open("rb") as handle:
        return TmfReference.model_validate(tomllib.load(handle))
