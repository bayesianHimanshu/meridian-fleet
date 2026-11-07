import pytest

from meridian.agents.base import (
    AgentConfig,
    Zone,
    zone_of,
)
from meridian.agents.rbqm.csm import Csm, CsmConfig
from meridian.agents.rbqm.kri_monitor import KriMonitor, KriMonitorConfig
from meridian.agents.registry import AgentRegistry, RegistrationError


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
