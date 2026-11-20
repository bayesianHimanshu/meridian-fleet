"""Which agents exist, which are enabled, and their configuration."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from meridian.agents.base import Agent, AgentConfig, zone_of


class RegistrationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RegisteredAgent:
    agent: Agent[Any]
    config: AgentConfig
    enabled: bool


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, RegisteredAgent] = {}

    def register(self, agent: Agent[Any], config: AgentConfig, *, enabled: bool = True) -> None:
        if zone_of(agent.agent_id) != agent.zone.value:
            raise RegistrationError(
                f"agent id {agent.agent_id!r} says zone {zone_of(agent.agent_id)!r} "
                f"but the agent declares zone {agent.zone.value!r}"
            )
        if agent.agent_id in self._agents:
            raise RegistrationError(f"agent {agent.agent_id!r} registered twice")
        if not isinstance(config, agent.config_model):
            raise RegistrationError(
                f"agent {agent.agent_id!r} needs a {agent.config_model.__name__}, "
                f"got {type(config).__name__}"
            )
        self._agents[agent.agent_id] = RegisteredAgent(agent, config, enabled)

    def all(self) -> list[RegisteredAgent]:
        return [self._agents[k] for k in sorted(self._agents)]

    def enabled(self) -> list[RegisteredAgent]:
        return [r for r in self.all() if r.enabled]

    def get(self, agent_id: str) -> RegisteredAgent:
        return self._agents[agent_id]

    def __iter__(self) -> Iterator[RegisteredAgent]:
        return iter(self.all())
