from datetime import UTC, timedelta
from pathlib import Path

import pytest

from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import FixedClock, SystemClock
from meridian.platform.config import CONFIG_FILE_ENV, Settings
from meridian.platform.container import Container
from tests.support import T0

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_system_clock_is_utc_aware() -> None:
    now = SystemClock().now()
    assert now.tzinfo is UTC


def test_fixed_clock_advances() -> None:
    clock = FixedClock(T0)
    clock.advance(timedelta(minutes=5))
    assert clock.now() == T0 + timedelta(minutes=5)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))


def test_settings_load_from_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_FILE_ENV, str(REPO_ROOT / "config" / "default.toml"))
    monkeypatch.delenv("MERIDIAN_LEDGER_PATH", raising=False)
    settings = Settings()
    assert settings.ledger_path == Path("var/ledger.sqlite3")
    assert settings.default_seed == 20261107
    assert settings.finding_id_prefixes == {
        "rbqm": "RBQM",
        "safety": "SAF",
        "quality": "QA",
        "preclinical": "PRE",
        "tmf": "TMF",
        "unblinded": "DSMB",
        "human": "OBS",
    }


def test_env_overrides_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(CONFIG_FILE_ENV, str(REPO_ROOT / "config" / "default.toml"))
    monkeypatch.setenv("MERIDIAN_LEDGER_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("MERIDIAN_DEFAULT_SEED", "7")
    settings = Settings()
    assert settings.ledger_path == tmp_path / "x.db"
    assert settings.default_seed == 7


def test_container_wires_injected_clock(tmp_path: Path) -> None:
    clock = FixedClock(T0)
    settings = Settings(ledger_path=tmp_path / "c.db")
    with Container.build(settings, clock=clock) as container:
        assert container.clock is clock
        assert isinstance(container.ledger, SqliteLedger)
        assert container.ledger.path == tmp_path / "c.db"


def test_container_defaults_to_system_clock(tmp_path: Path) -> None:
    with Container.build(Settings(ledger_path=tmp_path / "c.db")) as container:
        assert isinstance(container.clock, SystemClock)
