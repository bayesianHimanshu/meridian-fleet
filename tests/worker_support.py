"""A small staged cut for worker and executor tests."""

import uuid
from pathlib import Path

from meridian.domain.enums import Visibility
from meridian.domain.events import DataCut, Event
from tests.agent_support import clean
from tests.rbqm_support import NOW, STUDY, write_tables_cut


def tiny_cut(root: Path, cut_id: str = "DC-TINY") -> Path:
    """Write a clean synthetic cut (with unblinded/) and return its directory."""
    blinded, ops, unblinded = clean(per_site=8)
    study = write_tables_cut(root, cut_id, blinded, ops, unblinded)
    return study / cut_id


def cut_trigger(cut_id: str = "DC-TINY", seq: int = 7) -> Event:
    return Event(
        seq=seq,
        event_id=uuid.UUID("01890000-0000-7000-8000-000000000007"),
        occurred_at=NOW,
        recorded_at=NOW,
        actor="system.ingest",
        study_id=STUDY,
        visibility=Visibility.BLINDED,
        payload=DataCut(cut_id=cut_id, study_id=STUDY, path=f"curated/{STUDY}/{cut_id}"),
        prev_hash="0" * 64,
        hash="1" * 64,
    )
