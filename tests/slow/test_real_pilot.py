"""End to end against the real CDISC pilot files. Needs network: run with -m slow."""

from pathlib import Path

import httpx
import polars as pl
import pytest

from meridian.data.blinding import forbidden_columns_in
from meridian.data.cdisc_pilot import SDTM_DOMAINS
from meridian.data.fetch import fetch
from meridian.data.ingest import IngestService
from meridian.data.study_store import ParquetStudyReader, UnblindedReader
from meridian.data.xpt_reader import read_xpt
from meridian.domain.enums import EventType
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from tests.support import T0, make_ledger

pytestmark = pytest.mark.slow
REPO_ROOT = Path(__file__).resolve().parents[2]

# Rows per curated table and the source domain they must match.
SOURCE_OF = {
    "blinded/dm": "dm",
    "blinded/ae": "ae",
    "blinded/vs": "vs",
    "blinded/lb": "lb",
    "blinded/ds": "ds",
    "blinded/sv": "sv",
    "blinded/ex_dates": "ex",
    "unblinded/ex": "ex",
    "unblinded/randomization": "dm",
}


def test_fetch_ingest_and_verify_real_pilot(tmp_path: Path) -> None:
    settings = Settings()
    clock = FixedClock(T0)
    raw = tmp_path / "raw"
    with httpx.Client(follow_redirects=True, timeout=settings.pilot.timeout_seconds) as client:
        report = fetch(
            client=client,
            clock=clock,
            base_url=settings.pilot.base_url,
            domains=SDTM_DOMAINS,
            raw_dir=raw,
        )
    assert sorted(report.downloaded) == sorted(f"{d}.xpt" for d in SDTM_DOMAINS)

    with make_ledger(tmp_path / "ledger.sqlite3", clock) as ledger:
        service = IngestService(
            clock=clock,
            ledger=ledger,
            curated_root=tmp_path / "curated",
            sites_file=tmp_path / "sites.toml",
            study_id="MRD-201",
            ops_params=settings.ops_synth,
        )
        result = service.ingest_raw(raw, cut_id="DC-BASE", seed=settings.default_seed)
        assert verify(ledger).ok
        assert len(ledger.read(event_types=[EventType.DATA_CUT])) == 1

    # The generated mapping equals the one committed in config/sites.toml.
    assert (tmp_path / "sites.toml").read_text() == (
        REPO_ROOT / "config" / "sites.toml"
    ).read_text()

    source_rows = {d: read_xpt(raw / f"{d}.xpt").height for d in SDTM_DOMAINS}
    rows = {f.path.removesuffix(".parquet"): f.rows for f in result.manifest.files}
    for table, domain in SOURCE_OF.items():
        assert rows[table] == source_rows[domain], table
    for ops in ("data_entry", "queries", "deviations", "screening"):
        assert f"ops/{ops}" in rows
    assert rows["ops/data_entry"] == source_rows["sv"]

    reader = ParquetStudyReader(result.cut_path.parent, "DC-BASE")
    for name in reader.tables():
        assert forbidden_columns_in(reader.table(name).columns) == [], name
    assert UnblindedReader(result.cut_path.parent, "DC-BASE").tables() == ["ex", "randomization"]
    dm = reader.table("dm")
    assert dm["SITEID"].n_unique() == 17
    assert dm.height == 306
    assert set(pl.read_parquet(result.cut_path / "unblinded" / "randomization.parquet")["ARM"]) == {
        "Placebo",
        "Xanomeline High Dose",
        "Xanomeline Low Dose",
        "Screen Failure",
    }
