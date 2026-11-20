"""Independent verification of the hash chain, from raw stored bytes."""

import json

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.domain.events import Event
from meridian.ledger.hashing import GENESIS_HASH, canonical_json, compute_hash, envelope_material
from meridian.ledger.store import RawLedgerSource, RawRow


class VerificationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    events_checked: int
    first_broken_seq: int | None = None
    problem: str | None = None


class _BrokenRowError(Exception):
    pass


def _text(value: bytes, column: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _BrokenRowError(f"{column} is not valid UTF-8") from exc


def _check_row(row: RawRow, expected_seq: int, expected_prev_hash: str) -> str:
    """Return the row's hash if the row is sound, otherwise raise _BrokenRowError."""
    if row.seq != expected_seq:
        raise _BrokenRowError(f"seq gap: expected {expected_seq}, found {row.seq}")

    prev_hash = _text(row.prev_hash, "prev_hash")
    if prev_hash != expected_prev_hash:
        raise _BrokenRowError("prev_hash does not equal the previous event's hash")

    payload_text = _text(row.payload_json, "payload_json")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise _BrokenRowError(f"payload_json is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise _BrokenRowError("payload_json is not a JSON object")
    try:
        canonical = canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise _BrokenRowError(f"payload_json cannot be canonicalized: {exc}") from exc
    if canonical != payload_text:
        raise _BrokenRowError("payload_json is not in canonical form")

    event_type = _text(row.event_type, "event_type")
    if payload.get("type") != event_type:
        raise _BrokenRowError("event_type column does not match payload type")

    material = envelope_material(
        seq=row.seq,
        event_id=_text(row.event_id, "event_id"),
        occurred_at=_text(row.occurred_at, "occurred_at"),
        recorded_at=_text(row.recorded_at, "recorded_at"),
        actor=_text(row.actor, "actor"),
        study_id=None if row.study_id is None else _text(row.study_id, "study_id"),
        visibility=_text(row.visibility, "visibility"),
        payload=payload,
        prev_hash=prev_hash,
    )
    stored_hash = _text(row.hash, "hash")
    if compute_hash(prev_hash, material) != stored_hash:
        raise _BrokenRowError("hash does not recompute")

    try:
        Event.model_validate({**material, "hash": stored_hash})
    except ValidationError as exc:
        raise _BrokenRowError(
            f"row does not parse as an Event: {exc.error_count()} error(s)"
        ) from exc
    return stored_hash


def verify(ledger: RawLedgerSource) -> VerificationReport:
    """Check contiguous seq, prev_hash links and recomputed hashes. Stops at the first break."""
    checked = 0
    prev_hash = GENESIS_HASH
    for checked, row in enumerate(ledger.iter_raw_rows(), start=1):
        try:
            prev_hash = _check_row(row, expected_seq=checked, expected_prev_hash=prev_hash)
        except _BrokenRowError as broken:
            return VerificationReport(
                ok=False, events_checked=checked, first_broken_seq=row.seq, problem=str(broken)
            )
    return VerificationReport(ok=True, events_checked=checked)
