import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from meridian.domain.enums import EventType, Role, Visibility
from meridian.domain.events import Event, FindingRaised, RedactedEvent
from meridian.ledger.hashing import GENESIS_HASH
from meridian.ledger.store import (
    DuplicateDedupKeyError,
    FindingIdPreassignedError,
    LedgerNotInitializedError,
    LedgerReader,
    LedgerWriter,
    SqliteLedger,
    UnknownAgentZoneError,
)
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.platform.config import DEFAULT_FINDING_ID_PREFIXES
from tests.support import BLINDED_ROLES, STUDY, data_cut_draft, finding_draft


def test_sqlite_ledger_satisfies_protocols(ledger: SqliteLedger) -> None:
    writer: LedgerWriter = ledger
    reader: LedgerReader = ledger
    assert callable(writer.append)
    assert callable(reader.read)


def test_schema_is_strict_and_wal(ledger: SqliteLedger, ledger_path: Path) -> None:
    with sqlite3.connect(ledger_path) as conn:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        (sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
    assert mode == "wal"
    assert sql.rstrip().endswith("STRICT")


def test_update_is_rejected(ledger: SqliteLedger, ledger_path: Path) -> None:
    ledger.append(data_cut_draft(1))
    conn = sqlite3.connect(ledger_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="ledger is append-only"):
            conn.execute("UPDATE events SET actor = 'mallory' WHERE seq = 1")
    finally:
        conn.close()


def test_delete_is_rejected(ledger: SqliteLedger, ledger_path: Path) -> None:
    ledger.append(data_cut_draft(1))
    conn = sqlite3.connect(ledger_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="ledger is append-only"):
            conn.execute("DELETE FROM events WHERE seq = 1")
    finally:
        conn.close()
    assert len(ledger.read()) == 1


def test_out_of_chain_insert_is_rejected(ledger: SqliteLedger, ledger_path: Path) -> None:
    ledger.append(data_cut_draft(1))
    conn = sqlite3.connect(ledger_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="must extend the chain"):
            conn.execute(
                "INSERT INTO events VALUES (5, 'x', 'data_cut', 't', 't', 'a', NULL, "
                "'blinded', '{}', ?, ?)",
                (GENESIS_HASH, "f" * 64),
            )
    finally:
        conn.close()


def test_seq_has_no_gaps_after_200_appends(ledger: SqliteLedger, clock: FixedClock) -> None:
    for n in range(200):
        ledger.append(data_cut_draft(n))
        clock.advance(timedelta(seconds=1))
    events = ledger.read()
    assert [e.seq for e in events] == list(range(1, 201))
    assert verify(ledger).ok


def test_chain_links_and_clock_stamping(ledger: SqliteLedger, clock: FixedClock) -> None:
    first = ledger.append(data_cut_draft(1))
    clock.advance(timedelta(minutes=3))
    second = ledger.append(data_cut_draft(2))
    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.hash
    assert first.recorded_at == clock.now() - timedelta(minutes=3)
    assert second.recorded_at == clock.now()
    assert first.event_id.version == 7


def test_appended_event_equals_read_back(ledger: SqliteLedger) -> None:
    appended = ledger.append(finding_draft("k1"))
    (read_back,) = ledger.read()
    assert read_back == appended


def _finding_id(event: Event) -> str | None:
    assert isinstance(event.payload, FindingRaised)
    return event.payload.finding.finding_id


def test_finding_ids_are_issued_per_prefix(ledger: SqliteLedger) -> None:
    issued = [
        _finding_id(ledger.append(finding_draft(f"k{i}", agent_id=agent)))
        for i, agent in enumerate(
            [
                "rbqm.kri_sentinel",
                "rbqm.qtl_watch",
                "safety.ae_watch",
                "rbqm.kri_sentinel",
                "tmf.completeness",
                "safety.ae_watch",
            ]
        )
    ]
    assert issued == ["RBQM-0001", "RBQM-0002", "SAF-0001", "RBQM-0003", "TMF-0001", "SAF-0002"]


def test_duplicate_dedup_key_is_rejected(ledger: SqliteLedger) -> None:
    ledger.append(finding_draft("same-key"))
    with pytest.raises(DuplicateDedupKeyError, match="same-key") as info:
        ledger.append(finding_draft("same-key", agent_id="safety.ae_watch"))
    assert info.value.existing_seq == 1
    assert len(ledger.read()) == 1
    assert verify(ledger).ok
    # The failed append consumed no seq and no finding id.
    nxt = ledger.append(finding_draft("other-key"))
    assert nxt.seq == 2
    assert _finding_id(nxt) == "RBQM-0002"


def test_unknown_zone_is_rejected(ledger: SqliteLedger) -> None:
    with pytest.raises(UnknownAgentZoneError, match="martian"):
        ledger.append(finding_draft("k", agent_id="martian.probe"))
    assert ledger.read() == []


def test_preassigned_finding_id_is_rejected(ledger: SqliteLedger) -> None:
    draft = finding_draft("k")
    assert isinstance(draft.payload, FindingRaised)
    finding = draft.payload.finding.model_copy(update={"finding_id": "RBQM-0042"})
    tampered = draft.model_copy(
        update={"payload": draft.payload.model_copy(update={"finding": finding})}
    )
    with pytest.raises(FindingIdPreassignedError):
        ledger.append(tampered)


def test_find_by_dedup_key(ledger: SqliteLedger) -> None:
    ledger.append(data_cut_draft(1))
    raised = ledger.append(finding_draft("needle"))
    assert ledger.find_by_dedup_key("needle") == raised
    assert ledger.find_by_dedup_key("missing") is None


def _mixed(ledger: SqliteLedger) -> None:
    ledger.append(data_cut_draft(1))
    ledger.append(
        finding_draft("r", agent_id="unblinded.efficacy", visibility=Visibility.RESTRICTED)
    )
    ledger.append(finding_draft("b"))


@pytest.mark.parametrize("role", [*BLINDED_ROLES, None])
def test_blinded_roles_get_redacted_events(ledger: SqliteLedger, role: Role | None) -> None:
    _mixed(ledger)
    entries = ledger.read(viewer_role=role)
    assert [type(e) for e in entries] == [Event, RedactedEvent, Event]
    redacted = entries[1]
    assert isinstance(redacted, RedactedEvent)
    assert redacted.marker == "restricted"
    assert redacted.event_type is EventType.FINDING_RAISED
    assert redacted.prev_hash == entries[0].hash
    dumped = redacted.model_dump_json()
    assert "DSMB" not in dumped
    assert "payload" not in dumped


def test_unblinded_statistician_gets_full_events(ledger: SqliteLedger) -> None:
    _mixed(ledger)
    entries = ledger.read(viewer_role=Role.UNBLINDED_STATISTICIAN)
    assert all(isinstance(e, Event) for e in entries)
    restricted = entries[1]
    assert isinstance(restricted, Event)
    assert _finding_id(restricted) == "DSMB-0001"


def test_read_filters(ledger: SqliteLedger) -> None:
    _mixed(ledger)
    full = Role.UNBLINDED_STATISTICIAN
    assert [e.seq for e in ledger.read(since_seq=1, viewer_role=full)] == [2, 3]
    only_findings = ledger.read(event_types=[EventType.FINDING_RAISED], viewer_role=full)
    assert [e.seq for e in only_findings] == [2, 3]
    assert ledger.read(event_types=[]) == []
    assert len(ledger.read(study_id=STUDY)) == 3
    assert ledger.read(study_id="OTHER") == []


def test_verify_passes_on_clean_ledger(ledger: SqliteLedger) -> None:
    _mixed(ledger)
    report = verify(ledger)
    assert report.ok
    assert report.events_checked == 3
    assert report.first_broken_seq is None
    assert report.problem is None


def test_verify_passes_on_empty_ledger(ledger: SqliteLedger) -> None:
    assert verify(ledger).ok
    assert verify(ledger).events_checked == 0


def test_uninitialized_ledger_raises(tmp_path: Path, clock: FixedClock) -> None:
    ledger = SqliteLedger(tmp_path / "nope.db", clock, DEFAULT_FINDING_ID_PREFIXES)
    with pytest.raises(LedgerNotInitializedError, match="meridian ledger init"):
        ledger.read()
    assert not (tmp_path / "nope.db").exists()


def test_initialize_is_idempotent(ledger: SqliteLedger) -> None:
    ledger.append(data_cut_draft(1))
    ledger.initialize()
    assert len(ledger.read()) == 1


def test_invalid_prefix_is_rejected(tmp_path: Path, clock: FixedClock) -> None:
    with pytest.raises(ValueError, match="A-Z"):
        SqliteLedger(tmp_path / "x.db", clock, {"rbqm": "rb-qm"})
