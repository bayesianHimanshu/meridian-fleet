"""What meridian-signal-worker prints (phase 5 step 3a): the drafts, and every openFDA
response the run used, bodies included, so the host keeps them as the run's inputs."""

import json

from pydantic import BaseModel, ConfigDict

from meridian.agents.base import FindingDraft
from meridian.agents.execute import DraftsDocument
from meridian.ledger.hashing import canonical_json


class ResponseRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    query: str
    status: int
    body: str
    sha256: str
    last_updated: str


class SignalDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str
    agent_version: str
    drafts: tuple[FindingDraft, ...]
    replayed: bool  # the responses were staged (a replay), not fetched in this run
    responses: tuple[ResponseRecord, ...]

    def dumps(self) -> str:
        drafts = json.loads(
            DraftsDocument(
                agent_id=self.agent_id, agent_version=self.agent_version, drafts=self.drafts
            ).dumps()
        )["drafts"]
        data = self.model_dump(mode="json")
        data["drafts"] = drafts
        return canonical_json(data)

    @classmethod
    def loads(cls, text: str) -> SignalDocument:
        return cls.model_validate(json.loads(text))
