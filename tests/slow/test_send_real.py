"""The real SEND study end to end: fetch at the pinned commit, check every pinned sha256,
ingest twice into separate folders, byte for byte the same. Needs network: -m slow."""

from pathlib import Path

import httpx
import polars as pl
import pytest

from meridian.data.fetch import fetch
from meridian.data.send import ingest_send, send_study_events
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from tests.support import T0, make_ledger

pytestmark = pytest.mark.slow


def test_the_pinned_study_ingests_byte_identically(tmp_path: Path) -> None:
    p = Settings().preclinical
    raw = tmp_path / "raw"
    with httpx.Client(follow_redirects=True, timeout=p.timeout_seconds) as client:
        fetch(client=client, clock=FixedClock(T0), base_url=p.base_url,
              domains=sorted(p.files), raw_dir=raw)  # fmt: skip
    trees = []
    for name in ("a", "b"):
        ledger = make_ledger(tmp_path / f"{name}.sqlite3", FixedClock(T0))
        result = ingest_send(
            raw_dir=raw, pinned=p.files, send_root=tmp_path / name, study_id=p.study_id,
            cut_id=p.cut_id, source_study_id=p.source_study_id, treatment=p.treatment,
            source_treatment=p.source_treatment, ledger=ledger, occurred_at=T0,
        )  # fmt: skip
        assert result.cut.created
        assert result.event_appended
        assert len(send_study_events(ledger, p.study_id)) == 1
        root = tmp_path / name
        trees.append({q.relative_to(root): q.read_bytes() for q in root.rglob("*") if q.is_file()})
    assert trees[0] == trees[1]
    dm = pl.read_parquet(tmp_path / "a" / p.study_id / p.cut_id / "send" / "dm.parquet")
    assert dm.height == 150
    assert dm["STUDYID"].unique().to_list() == [p.study_id]
