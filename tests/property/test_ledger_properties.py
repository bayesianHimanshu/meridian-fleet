import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from meridian.domain.events import EventDraft
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from tests.property.strategies import drafts, with_unique_dedup_key
from tests.support import T0, make_ledger

SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
gaps = st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=3))


def _fill(
    ledger: SqliteLedger, clock: FixedClock, items: list[tuple[EventDraft, timedelta]]
) -> None:
    for index, (draft, gap) in enumerate(items):
        clock.advance(gap)
        ledger.append(with_unique_dedup_key(draft, index))


def _tampered_copy(source: Path, target: Path, seq: int, byte_index: int, delta: int) -> None:
    """Copy the ledger, strip its protections in the copy, and flip one payload byte."""
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    conn = sqlite3.connect(target, autocommit=True)
    try:
        for kind in ("trigger", "index"):
            names = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = ? AND sql IS NOT NULL", (kind,)
            ).fetchall()
            for (name,) in names:
                conn.execute(f'DROP {kind.upper()} "{name}"')
        (raw,) = conn.execute(
            "SELECT CAST(payload_json AS BLOB) FROM events WHERE seq = ?", (seq,)
        ).fetchone()
        data = bytearray(raw)
        position = byte_index % len(data)
        data[position] = (data[position] + delta) % 256
        conn.execute(
            "UPDATE events SET payload_json = CAST(? AS TEXT) WHERE seq = ?", (bytes(data), seq)
        )
    finally:
        conn.close()


@SETTINGS
@given(items=st.lists(st.tuples(drafts(), gaps), max_size=15))
def test_verify_passes_for_any_sequence_of_drafts(
    items: list[tuple[EventDraft, timedelta]],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clock = FixedClock(T0)
        with make_ledger(Path(tmp) / "l.db", clock) as ledger:
            _fill(ledger, clock, items)
            report = verify(ledger)
            assert report.ok, report
            assert report.events_checked == len(items)


@SETTINGS
@given(
    items=st.lists(st.tuples(drafts(), gaps), min_size=1, max_size=10),
    data=st.data(),
)
def test_any_single_byte_change_in_payload_is_caught_at_that_seq(
    items: list[tuple[EventDraft, timedelta]], data: st.DataObject
) -> None:
    seq = data.draw(st.integers(1, len(items)), label="seq")
    byte_index = data.draw(st.integers(0, 10**6), label="byte_index")
    delta = data.draw(st.integers(1, 255), label="delta")
    with tempfile.TemporaryDirectory() as tmp:
        clean, dirty = Path(tmp) / "clean.db", Path(tmp) / "dirty.db"
        clock = FixedClock(T0)
        with make_ledger(clean, clock) as ledger:
            _fill(ledger, clock, items)
            assert verify(ledger).ok
            _tampered_copy(clean, dirty, seq, byte_index, delta)
        with SqliteLedger(dirty, clock, {}) as tampered:
            report = verify(tampered)
        assert not report.ok
        assert report.first_broken_seq == seq, report
        assert report.events_checked == seq
