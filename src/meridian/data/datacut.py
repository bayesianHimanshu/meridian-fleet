"""Find and record DataCut events. Shared by ingest and the fault injector."""

from pathlib import Path
from typing import Protocol

from meridian.domain.enums import EventType, Visibility
from meridian.domain.events import DataCut, Event, EventDraft
from meridian.ledger.store import LedgerReader, LedgerWriter
from meridian.platform.clock import Clock


class Ledger(LedgerReader, LedgerWriter, Protocol):
    pass


def data_cut_events(reader: LedgerReader, study_id: str | None = None) -> list[Event]:
    """Every DataCut event in seq order, optionally only for one study."""
    events: list[Event] = []
    for entry in reader.read(event_types=[EventType.DATA_CUT]):
        if not isinstance(entry, Event) or not isinstance(entry.payload, DataCut):
            continue
        if study_id is None or entry.payload.study_id == study_id:
            events.append(entry)
    return events


def find_data_cut(reader: LedgerReader, study_id: str, cut_id: str) -> Event | None:
    for event in data_cut_events(reader, study_id):
        assert isinstance(event.payload, DataCut)
        if event.payload.cut_id == cut_id:
            return event
    return None


def record_data_cut(
    ledger: Ledger, *, clock: Clock, actor: str, study_id: str, cut_id: str, path: Path
) -> tuple[Event, bool]:
    """Append a blinded DataCut once. Returns (event, appended)."""
    existing = find_data_cut(ledger, study_id, cut_id)
    if existing is not None:
        return existing, False
    event = ledger.append(
        EventDraft(
            occurred_at=clock.now(),
            actor=actor,
            study_id=study_id,
            visibility=Visibility.BLINDED,
            payload=DataCut(cut_id=cut_id, study_id=study_id, path=path.as_posix()),
        )
    )
    return event, True
