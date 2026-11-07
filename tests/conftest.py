from collections.abc import Iterator
from pathlib import Path

import pytest

from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import FixedClock
from tests.support import T0, make_ledger


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.sqlite3"


@pytest.fixture
def ledger(ledger_path: Path, clock: FixedClock) -> Iterator[SqliteLedger]:
    with make_ledger(ledger_path, clock) as opened:
        yield opened
