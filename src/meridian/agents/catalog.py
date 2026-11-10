"""Every known agent and the registry built from settings.

Kept apart from the Container so that meridian-worker can build the same registry
without importing the ledger store, ingestion, faults or evals.
"""

from typing import Any

from meridian.agents.base import Agent, AgentConfig
from meridian.agents.quality.tmf_completeness import TmfCompleteness
from meridian.agents.rbqm.csm import Csm
from meridian.agents.rbqm.kri_monitor import KriMonitor
from meridian.agents.rbqm.qtl_detector import QtlDetector
from meridian.agents.registry import AgentRegistry, RegistrationError
from meridian.agents.safety.sae_reconciler import SaeReconciler
from meridian.platform.config import AgentsSettings


def build_registry(cfg: AgentsSettings) -> AgentRegistry:
    """Every known agent, with its config from settings; enabled per settings."""
    known: list[tuple[Agent[Any], AgentConfig]] = [
        (KriMonitor(), cfg.kri_monitor),
        (QtlDetector(), cfg.qtl_detector),
        (Csm(), cfg.csm),
        (SaeReconciler(), cfg.sae_reconciler),
        (TmfCompleteness(), cfg.tmf_completeness),
    ]
    unknown = sorted(set(cfg.enabled) - {agent.agent_id for agent, _ in known})
    if unknown:
        raise RegistrationError(f"enabled agents not known: {unknown}")
    registry = AgentRegistry()
    for agent, config in known:
        registry.register(agent, config, enabled=agent.agent_id in cfg.enabled)
    return registry
