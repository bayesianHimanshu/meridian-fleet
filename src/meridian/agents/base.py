"""Agent protocol, context, finding drafts and the dedup key format."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, NamedTuple, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from meridian.data.study_store import StudyReader
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event
from meridian.domain.findings import Evidence
from meridian.platform.clock import Clock

STUDY_SCOPE = "STUDY"
NO_METRIC = "-"
_SEP = "|"


class Zone(StrEnum):
    PORTFOLIO = "portfolio"
    PRECLINICAL = "preclinical"
    STUDY_OPS = "study_ops"
    RBQM = "rbqm"
    SAFETY = "safety"
    QUALITY = "quality"
    UNBLINDED = "unblinded"


class AgentConfig(BaseModel):
    """Base for each agent's own configuration model."""

    model_config = ConfigDict(frozen=True, extra="forbid")


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


class DedupKeyParts(NamedTuple):
    agent_id: str
    kind: str
    study_id: str
    site: str
    cut_id: str
    metric: str


def make_dedup_key(
    *,
    agent_id: str,
    kind: FindingKind,
    study_id: str,
    site_id: str | None,
    cut_id: str,
    metric: str | None,
) -> str:
    """'<agent_id>|<kind>|<study_id>|<site_id or STUDY>|<cut_id>|<kri or ->'."""
    parts = (
        agent_id,
        kind.value,
        study_id,
        site_id or STUDY_SCOPE,
        cut_id,
        metric or NO_METRIC,
    )
    for part in parts:
        if not part or _SEP in part:
            raise ValueError(f"dedup key part {part!r} is empty or contains {_SEP!r}")
    return _SEP.join(parts)


def parse_dedup_key(key: str) -> DedupKeyParts:
    parts = key.split(_SEP)
    if len(parts) != len(DedupKeyParts._fields):
        raise ValueError(f"not a dedup key: {key!r}")
    return DedupKeyParts(*parts)


def zone_of(agent_id: str) -> str:
    return agent_id.split(".", 1)[0]
