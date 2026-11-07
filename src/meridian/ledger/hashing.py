"""Canonical serialization and the hash chain.

canonical_json is the single serializer used both for hashing and for the stored
payload_json column, so the bytes on disk are exactly the bytes that were hashed.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel

GENESIS_HASH = "0" * 64


def format_utc(value: datetime) -> str:
    """ISO 8601 in UTC with microsecond precision and a trailing Z."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("refusing to serialize a naive datetime")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _normalize(obj: object) -> object:
    match obj:
        case None | bool():
            return obj
        case Enum():
            return _normalize(obj.value)
        case str() | int():
            return obj
        case float():
            if not math.isfinite(obj):
                raise ValueError(f"non-finite float cannot be canonicalized: {obj!r}")
            return obj
        case datetime():
            return format_utc(obj)
        case UUID():
            return str(obj)
        case BaseModel():
            return _normalize(obj.model_dump(mode="python"))
        case Mapping():
            out: dict[str, object] = {}
            for key, value in obj.items():
                if not isinstance(key, str):
                    raise TypeError(f"mapping keys must be str, got {type(key).__name__}")
                out[key] = _normalize(value)
            return out
        case list() | tuple():
            return [_normalize(item) for item in obj]
        case set() | frozenset():
            items = [_normalize(item) for item in obj]
            return sorted(items, key=_dumps)
        case _:
            raise TypeError(f"cannot canonicalize {type(obj).__name__}")


def _dumps(obj: object) -> str:
    # json.dumps writes floats with float.__repr__, which round-trips exactly.
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_json(obj: object) -> str:
    """Sorted keys, no whitespace, datetimes as ISO 8601 ending in Z, floats via repr.

    Sets are emitted as lists sorted by their canonical form. The result is encoded
    as UTF-8 wherever it becomes bytes.
    """
    return _dumps(_normalize(obj))


def envelope_material(
    *,
    seq: int,
    event_id: str,
    occurred_at: str,
    recorded_at: str,
    actor: str,
    study_id: str | None,
    visibility: str,
    payload: object,
    prev_hash: str,
) -> dict[str, object]:
    """The event without its hash: exactly what gets hashed.

    Both the writer (from the domain Event) and the verifier (from raw stored columns)
    build the hash input through this one function.
    """
    return {
        "seq": seq,
        "event_id": event_id,
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "actor": actor,
        "study_id": study_id,
        "visibility": visibility,
        "payload": payload,
        "prev_hash": prev_hash,
    }


def compute_hash(prev_hash: str, event_without_hash: object) -> str:
    """Hex SHA-256 of prev_hash followed by the canonical JSON of the event."""
    data = (prev_hash + canonical_json(event_without_hash)).encode("utf-8")
    return hashlib.sha256(data).hexdigest()
