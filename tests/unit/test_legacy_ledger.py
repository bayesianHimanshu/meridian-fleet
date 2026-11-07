"""Ledgers written before ActorWoke/ActorSuspended carried agent_version still verify."""

import sqlite3
import uuid
from pathlib import Path

from meridian.domain.enums import EventType, Visibility
from meridian.domain.events import ActorSuspended, ActorWoke, Event, EventDraft
from meridian.ledger.hashing import (
    GENESIS_HASH,
    canonical_json,
    compute_hash,
    envelope_material,
    format_utc,
)
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from tests.support import T0, data_cut_draft, make_ledger


def _insert_legacy(path: Path, seq: int, prev_hash: str, payload: dict[str, object]) -> str:
    """Write a row exactly as the step 3 code did: no agent_version key in the payload."""
    event_id = str(uuid.uuid7())
    when = format_utc(T0)
    material = envelope_material(
        seq=seq,
        event_id=event_id,
        occurred_at=when,
        recorded_at=when,
        actor="rbqm.csm",
        study_id="MER-101",
        visibility="blinded",
        payload=payload,
        prev_hash=prev_hash,
    )
    digest = compute_hash(prev_hash, material)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seq,
                event_id,
                payload["type"],
                when,
                when,
                "rbqm.csm",
                "MER-101",
                "blinded",
                canonical_json(payload),
                prev_hash,
                digest,
            ),
        )
    return digest


def test_legacy_events_verify_and_new_events_append(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    clock = FixedClock(T0)
    with make_ledger(path, clock) as ledger:
        cut = ledger.append(data_cut_draft(1))
    # Legacy-shaped events, written by "old code".
    h = _insert_legacy(
        path, 2, cut.hash, {"type": "actor_woke", "agent_id": "rbqm.csm", "trigger_seq": cut.seq}
    )
    _insert_legacy(
        path, 3, h, {"type": "actor_suspended", "agent_id": "rbqm.csm", "trigger_seq": cut.seq}
    )
    with sqlite3.connect(path) as conn:
        payloads = [r[0] for r in conn.execute("SELECT payload_json FROM events WHERE seq > 1")]
    assert all("agent_version" not in p for p in payloads)

    # "Upgraded" code opens the same file, reads, appends versioned events, verifies.
    with make_ledger(path, clock) as ledger:
        assert verify(ledger).ok
        woke = ledger.read(event_types=[EventType.ACTOR_WOKE])[0]
        assert isinstance(woke, Event)
        assert isinstance(woke.payload, ActorWoke)
        assert woke.payload.agent_version is None
        for payload in (
            ActorWoke(agent_id="rbqm.csm", agent_version="2.0.0", trigger_seq=cut.seq),
            ActorSuspended(agent_id="rbqm.csm", agent_version="2.0.0", trigger_seq=cut.seq),
        ):
            ledger.append(
                EventDraft(
                    occurred_at=T0, actor="rbqm.csm", visibility=Visibility.BLINDED, payload=payload
                )
            )
        report = verify(ledger)
        assert report.ok, report
        assert report.events_checked == 5
        assert ledger.read()[-1].prev_hash != GENESIS_HASH
