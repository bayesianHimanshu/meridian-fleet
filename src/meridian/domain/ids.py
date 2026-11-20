"""Identifier and primitive value types shared across the domain."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, StringConstraints

StudyId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
SiteId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
AgentId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ActorId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
FindingId = Annotated[str, StringConstraints(pattern=r"^[A-Z]+-[0-9]{4}$")]
DedupKey = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
# A content digest as docker reports it: "sha256:" plus 64 hex characters.
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""A timezone-aware datetime, normalized to UTC. Naive datetimes are rejected."""
