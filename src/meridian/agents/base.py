"""Agent protocol, context, finding drafts and the dedup key format."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from meridian.data.study_store import StudyReader
from meridian.domain.dedup import STUDY_SCOPE, DedupKeyParts, make_dedup_key, parse_dedup_key
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import AgentConfig
from meridian.platform.clock import Clock

__all__ = [
    "STUDY_SCOPE",
    "Agent",
    "AgentConfig",
    "AgentContext",
    "DedupKeyParts",
    "FindingDraft",
    "Zone",
    "make_dedup_key",
    "parse_dedup_key",
    "zone_of",
]


class Zone(StrEnum):
    PORTFOLIO = "portfolio"
    PRECLINICAL = "preclinical"
    STUDY_OPS = "study_ops"
    RBQM = "rbqm"
    SAFETY = "safety"
    QUALITY = "quality"
    UNBLINDED = "unblinded"


@dataclass(frozen=True, slots=True)
class AgentContext[C: AgentConfig]:
    study: StudyReader
    clock: Clock
    config: C
    log: structlog.typing.FilteringBoundLogger


class FindingDraft(BaseModel):
    """What an agent returns. The runner turns it into a Finding and fills in identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dedup_key: Annotated[str, Field(min_length=1)]
    kind: FindingKind
    site_id: str | None
    severity: Severity
    title: Annotated[str, Field(min_length=1)]
    summary: Annotated[str, Field(min_length=1)]
    evidence: Annotated[tuple[Evidence, ...], Field(min_length=1)]
    score: FiniteFloat


class Agent[C: AgentConfig](Protocol):
    @property
    def agent_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def zone(self) -> Zone: ...

    @property
    def subscribes_to(self) -> frozenset[EventType]: ...

    @property
    def visibility(self) -> Visibility: ...

    @property
    def visible_to(self) -> frozenset[Role]: ...

    @property
    def config_model(self) -> type[C]: ...

    def run(self, trigger: Event, ctx: AgentContext[C]) -> Sequence[FindingDraft]: ...


def zone_of(agent_id: str) -> str:
    return agent_id.split(".", 1)[0]
