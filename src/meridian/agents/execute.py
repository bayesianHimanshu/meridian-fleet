"""One agent run as a pure function, and the drafts document a worker prints.

Shared by the in-process executor and meridian-worker, so both produce the same drafts
from the same inputs. Nothing here touches the ledger.
"""

import json
from collections.abc import Sequence
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.registry import RegisteredAgent
from meridian.data.study_store import StudyReader
from meridian.domain.events import Event
from meridian.ledger.hashing import canonical_json
from meridian.platform.clock import FixedClock


class DraftsDocument(BaseModel):
    """What a worker prints on stdout: one canonical JSON document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str
    agent_version: str
    drafts: tuple[FindingDraft, ...]

    def dumps(self) -> str:
        data = self.model_dump(mode="json")
        for draft in data["drafts"]:
            if draft["visible_to"] is None:
                del draft["visible_to"]  # documents of agents without it stay as they were
        return canonical_json(data)

    @classmethod
    def loads(cls, text: str) -> DraftsDocument:
        return cls.model_validate(json.loads(text))


def run_agent(
    registered: RegisteredAgent,
    trigger: Event,
    study: StudyReader,
    log: structlog.typing.FilteringBoundLogger,
) -> list[FindingDraft]:
    """Run one agent on one trigger. The clock is fixed at the trigger's recorded_at, so
    the same trigger and data always give the same drafts, wherever this runs."""
    ctx: AgentContext[Any] = AgentContext(
        study=study, clock=FixedClock(trigger.recorded_at), config=registered.config, log=log
    )
    return list(registered.agent.run(trigger, ctx))


def drafts_document(registered: RegisteredAgent, drafts: Sequence[FindingDraft]) -> str:
    return DraftsDocument(
        agent_id=registered.agent.agent_id,
        agent_version=registered.agent.version,
        drafts=tuple(drafts),
    ).dumps()
