import pytest

from meridian.agents.base import (
    AgentConfig,
    Zone,
    make_dedup_key,
    parse_dedup_key,
    zone_of,
)
from meridian.agents.rbqm.csm import Csm, CsmConfig
from meridian.agents.rbqm.kri_monitor import KriMonitor, KriMonitorConfig
from meridian.agents.registry import AgentRegistry, RegistrationError
from meridian.domain.enums import FindingKind


def test_dedup_key_format_round_trips() -> None:
    key = make_dedup_key(
        agent_id="rbqm.kri_monitor",
        kind=FindingKind.KRI_BREACH,
        study_id="MRD-201",
        site_id="1004",
        cut_id="DC-BASE",
        metric="data_entry_lag",
    )
    assert key == "rbqm.kri_monitor|kri_breach|MRD-201|1004|DC-BASE|data_entry_lag"
    assert parse_dedup_key(key).metric == "data_entry_lag"
    study_level = make_dedup_key(
        agent_id="rbqm.csm",
        kind=FindingKind.QTL_BREACH,
        study_id="MRD-201",
        site_id=None,
        cut_id="DC-X",
        metric=None,
    )
    assert study_level.endswith("|STUDY|DC-X|-")


def test_dedup_key_rejects_separator() -> None:
    with pytest.raises(ValueError, match="contains"):
        make_dedup_key(
            agent_id="rbqm.x",
            kind=FindingKind.KRI_BREACH,
            study_id="A|B",
            site_id=None,
            cut_id="C",
            metric=None,
        )


class _WrongZone(KriMonitor):
    agent_id = "safety.kri_monitor"


def test_registration_checks_zone_matches_agent_id() -> None:
    registry = AgentRegistry()
    with pytest.raises(RegistrationError, match="zone"):
        registry.register(_WrongZone(), KriMonitorConfig())
    assert zone_of("rbqm.kri_monitor") == Zone.RBQM.value


def test_registration_rejects_duplicates_and_wrong_config() -> None:
    registry = AgentRegistry()
    registry.register(KriMonitor(), KriMonitorConfig())
    with pytest.raises(RegistrationError, match="twice"):
        registry.register(KriMonitor(), KriMonitorConfig())
    with pytest.raises(RegistrationError, match="CsmConfig"):
        registry.register(Csm(), AgentConfig())


def test_registry_lists_enabled_in_id_order() -> None:
    registry = AgentRegistry()
    registry.register(KriMonitor(), KriMonitorConfig(), enabled=False)
    registry.register(Csm(), CsmConfig())
    assert [r.agent.agent_id for r in registry.all()] == ["rbqm.csm", "rbqm.kri_monitor"]
    assert [r.agent.agent_id for r in registry.enabled()] == ["rbqm.csm"]
