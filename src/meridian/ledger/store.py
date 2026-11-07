"""SQLite-backed append-only ledger."""

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

from meridian.domain.enums import EventType, Role, Visibility
from meridian.domain.events import (
    Event,
    EventDraft,
    EventPayload,
    FindingRaised,
    LedgerEntry,
    RedactedEvent,
)
from meridian.ledger.hashing import (
    GENESIS_HASH,
    canonical_json,
    compute_hash,
    envelope_material,
    format_utc,
)
from meridian.platform.clock import Clock

FINDING_NUMBER_DIGITS = 4
_MAX_FINDING_NUMBER = 10**FINDING_NUMBER_DIGITS - 1
_PREFIX_RE = re.compile(r"^[A-Z]+$")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY,
    event_id     TEXT UNIQUE NOT NULL,
    event_type   TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    actor        TEXT NOT NULL,
    study_id     TEXT,
    visibility   TEXT NOT NULL CHECK (visibility IN ('blinded', 'restricted')),
    payload_json TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL UNIQUE
) STRICT;

CREATE INDEX IF NOT EXISTS ix_events_type_seq ON events (event_type, seq);
CREATE INDEX IF NOT EXISTS ix_events_study_seq ON events (study_id, seq);

CREATE UNIQUE INDEX IF NOT EXISTS ux_events_dedup_key
    ON events (json_extract(payload_json, '$.dedup_key'))
    WHERE event_type = 'finding_raised';

CREATE UNIQUE INDEX IF NOT EXISTS ux_events_finding_id
    ON events (json_extract(payload_json, '$.finding.finding_id'))
    WHERE event_type = 'finding_raised';

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_insert_extends_chain
BEFORE INSERT ON events
WHEN NEW.seq IS NOT COALESCE((SELECT MAX(seq) FROM events), 0) + 1
  OR NEW.prev_hash IS NOT COALESCE(
        (SELECT hash FROM events ORDER BY seq DESC LIMIT 1), '__GENESIS_HASH__')
BEGIN
    SELECT RAISE(ABORT, 'ledger insert must extend the chain');
END;
"""

SCHEMA_SQL = SCHEMA_SQL.replace("__GENESIS_HASH__", GENESIS_HASH)

_INSERT_SQL = """
INSERT INTO events (
    seq, event_id, event_type, occurred_at, recorded_at, actor,
    study_id, visibility, payload_json, prev_hash, hash
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_COLUMNS = (
    "seq, event_id, event_type, occurred_at, recorded_at, actor, "
    "study_id, visibility, payload_json, prev_hash, hash"
)


class LedgerError(Exception):
    """Base class for ledger failures."""


class LedgerNotInitializedError(LedgerError):
    pass


class DuplicateDedupKeyError(LedgerError):
    def __init__(self, dedup_key: str, existing_seq: int) -> None:
        super().__init__(
            f"a finding with dedup_key {dedup_key!r} already exists at seq {existing_seq}"
        )
        self.dedup_key = dedup_key
        self.existing_seq = existing_seq


class UnknownAgentZoneError(LedgerError):
    pass


class FindingIdPreassignedError(LedgerError):
    pass


class FindingIdExhaustedError(LedgerError):
    pass


class LedgerWriter(Protocol):
    def append(self, draft: EventDraft) -> Event: ...


class LedgerReader(Protocol):
    def read(
        self,
        since_seq: int = 0,
        event_types: Collection[EventType] | None = None,
        study_id: str | None = None,
        viewer_role: Role | None = None,
    ) -> list[LedgerEntry]: ...

    def find_by_dedup_key(self, key: str) -> Event | None: ...


@dataclass(frozen=True, slots=True)
class RawRow:
    """A stored row exactly as bytes, for verification without trusting decoding."""

    seq: int
    event_id: bytes
    event_type: bytes
    occurred_at: bytes
    recorded_at: bytes
    actor: bytes
    study_id: bytes | None
    visibility: bytes
    payload_json: bytes
    prev_hash: bytes
    hash: bytes


_Row = tuple[Any, ...]


class RawLedgerSource(Protocol):
    def iter_raw_rows(self) -> Iterator[RawRow]: ...


def agent_zone(agent_id: str) -> str:
    """Agent ids have the form "<zone>.<name>", for example "rbqm.kri_sentinel"."""
    return agent_id.split(".", 1)[0]


class SqliteLedger:
    """Implements LedgerWriter, LedgerReader and RawLedgerSource on one SQLite file."""

    def __init__(
        self,
        path: Path,
        clock: Clock,
        finding_id_prefixes: Mapping[str, str],
        new_event_id: Callable[[], uuid.UUID] = uuid.uuid7,
    ) -> None:
        for zone, prefix in finding_id_prefixes.items():
            if not _PREFIX_RE.fullmatch(prefix):
                raise ValueError(f"finding id prefix for zone {zone!r} must be A-Z: {prefix!r}")
        self._path = path
        self._clock = clock
        self._prefixes = dict(finding_id_prefixes)
        self._new_event_id = new_event_id
        self._conn: sqlite3.Connection | None = None
        self._schema_checked = False

    @property
    def path(self) -> Path:
        return self._path

    # Lifecycle

    def initialize(self) -> None:
        """Create the database file, schema and triggers. Safe to call repeatedly."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)
        self._schema_checked = True

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self._path, autocommit=True)
            conn.execute("PRAGMA busy_timeout = 5000")
            # Makes REPLACE conflict resolution fire the delete trigger too.
            conn.execute("PRAGMA recursive_triggers = ON")
            self._conn = conn
        return self._conn

    def _ready(self) -> sqlite3.Connection:
        if not self._schema_checked:
            if not self._path.exists():
                raise LedgerNotInitializedError(
                    f"no ledger at {self._path}; run `meridian ledger init` first"
                )
            conn = self._connect()
            found = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
            ).fetchone()
            if found is None:
                raise LedgerNotInitializedError(
                    f"{self._path} has no events table; run `meridian ledger init` first"
                )
            self._schema_checked = True
        return self._connect()

    # Writing

    def append(self, draft: EventDraft) -> Event:
        return self.append_guarded(draft, lambda: None)

    def append_guarded(self, draft: EventDraft, guard: Callable[[], None]) -> Event:
        """Append draft only if guard() returns without raising.

        guard runs inside the same write transaction (BEGIN IMMEDIATE), so what it reads
        through this ledger cannot change before the append: no other writer, in this
        process or another, can commit in between. Whatever guard raises propagates and
        nothing is written.
        """
        conn = self._ready()
        conn.execute("BEGIN IMMEDIATE")
        try:
            guard()
            event = self._seal(conn, draft)
            conn.execute(_INSERT_SQL, _row_values(event))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return event

    def _seal(self, conn: sqlite3.Connection, draft: EventDraft) -> Event:
        last = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev_hash = (1, GENESIS_HASH) if last is None else (int(last[0]) + 1, str(last[1]))

        payload: EventPayload = draft.payload
        if isinstance(payload, FindingRaised):
            payload = self._issue_finding_id(conn, payload)

        event_id = self._new_event_id()
        recorded_at = self._clock.now()
        material = envelope_material(
            seq=seq,
            event_id=str(event_id),
            occurred_at=format_utc(draft.occurred_at),
            recorded_at=format_utc(recorded_at),
            actor=draft.actor,
            study_id=draft.study_id,
            visibility=draft.visibility.value,
            payload=payload,
            prev_hash=prev_hash,
        )
        return Event(
            seq=seq,
            event_id=event_id,
            occurred_at=draft.occurred_at,
            recorded_at=recorded_at,
            actor=draft.actor,
            study_id=draft.study_id,
            visibility=draft.visibility,
            payload=payload,
            prev_hash=prev_hash,
            hash=compute_hash(prev_hash, material),
        )

    def _issue_finding_id(self, conn: sqlite3.Connection, payload: FindingRaised) -> FindingRaised:
        if payload.finding.finding_id is not None:
            raise FindingIdPreassignedError(
                "finding_id is issued by the ledger; submit the finding with finding_id=None"
            )
        existing = self._find_dedup_row(conn, payload.dedup_key)
        if existing is not None:
            raise DuplicateDedupKeyError(payload.dedup_key, int(existing[0]))

        zone = agent_zone(payload.finding.agent_id)
        prefix = self._prefixes.get(zone)
        if prefix is None:
            raise UnknownAgentZoneError(
                f"agent {payload.finding.agent_id!r} is in zone {zone!r}, which has no "
                f"finding id prefix; known zones: {sorted(self._prefixes)}"
            )
        pattern = prefix + "-" + "[0-9]" * FINDING_NUMBER_DIGITS
        (highest,) = conn.execute(
            "SELECT MAX(json_extract(payload_json, '$.finding.finding_id')) FROM events "
            "WHERE event_type = 'finding_raised' "
            "AND json_extract(payload_json, '$.finding.finding_id') GLOB ?",
            (pattern,),
        ).fetchone()
        number = 1 if highest is None else int(str(highest).rsplit("-", 1)[1]) + 1
        if number > _MAX_FINDING_NUMBER:
            raise FindingIdExhaustedError(f"finding ids for prefix {prefix} are exhausted")
        finding_id = f"{prefix}-{number:0{FINDING_NUMBER_DIGITS}d}"
        finding = payload.finding.model_copy(update={"finding_id": finding_id})
        return payload.model_copy(update={"finding": finding})

    # Reading

    def read(
        self,
        since_seq: int = 0,
        event_types: Collection[EventType] | None = None,
        study_id: str | None = None,
        viewer_role: Role | None = None,
    ) -> list[LedgerEntry]:
        """Events with seq > since_seq, in seq order.

        Unless viewer_role is unblinded_statistician (and that includes viewer_role=None),
        restricted events are returned as RedactedEvent.
        """
        conn = self._ready()
        clauses = ["seq > ?"]
        params: list[Any] = [since_seq]
        if event_types is not None:
            types = sorted({EventType(t).value for t in event_types})
            if not types:
                return []
            clauses.append("event_type IN (" + ", ".join("?" for _ in types) + ")")
            params.extend(types)
        if study_id is not None:
            clauses.append("study_id = ?")
            params.append(study_id)
        sql = f"SELECT {_SELECT_COLUMNS} FROM events WHERE {' AND '.join(clauses)} ORDER BY seq"  # noqa: S608 (only fixed fragments and placeholders)
        full_access = viewer_role is Role.UNBLINDED_STATISTICIAN
        entries: list[LedgerEntry] = []
        for row in conn.execute(sql, params):
            if row[7] == Visibility.RESTRICTED.value and not full_access:
                entries.append(_redacted_from_row(row))
            else:
                entries.append(_event_from_row(row))
        return entries

    def find_by_dedup_key(self, key: str) -> Event | None:
        row = self._find_dedup_row(self._ready(), key)
        return None if row is None else _event_from_row(row)

    def _find_dedup_row(self, conn: sqlite3.Connection, key: str) -> _Row | None:
        row: _Row | None = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM events "  # noqa: S608 (fixed column list)
            "WHERE event_type = 'finding_raised' "
            "AND json_extract(payload_json, '$.dedup_key') = ?",
            (key,),
        ).fetchone()
        return row

    def iter_raw_rows(self) -> Iterator[RawRow]:
        """Every stored row in seq order, text columns as raw bytes. Never redacted."""
        conn = self._ready()
        cursor = conn.execute(
            "SELECT seq, CAST(event_id AS BLOB), CAST(event_type AS BLOB), "
            "CAST(occurred_at AS BLOB), CAST(recorded_at AS BLOB), CAST(actor AS BLOB), "
            "CAST(study_id AS BLOB), CAST(visibility AS BLOB), CAST(payload_json AS BLOB), "
            "CAST(prev_hash AS BLOB), CAST(hash AS BLOB) FROM events ORDER BY seq"
        )
        for row in cursor:
            yield RawRow(*row)


def _row_values(event: Event) -> tuple[object, ...]:
    return (
        event.seq,
        str(event.event_id),
        event.event_type.value,
        format_utc(event.occurred_at),
        format_utc(event.recorded_at),
        event.actor,
        event.study_id,
        event.visibility.value,
        canonical_json(event.payload),
        event.prev_hash,
        event.hash,
    )


def _event_from_row(row: _Row) -> Event:
    return Event.model_validate(
        {
            "seq": row[0],
            "event_id": row[1],
            "occurred_at": row[3],
            "recorded_at": row[4],
            "actor": row[5],
            "study_id": row[6],
            "visibility": row[7],
            "payload": json.loads(row[8]),
            "prev_hash": row[9],
            "hash": row[10],
        }
    )


def _redacted_from_row(row: _Row) -> RedactedEvent:
    return RedactedEvent(
        seq=row[0],
        event_type=EventType(row[2]),
        recorded_at=row[4],
        hash=row[10],
        prev_hash=row[9],
    )
