"""Every known agent and the registry built from settings.

Kept apart from the Container so that meridian-worker can build the same registry
without importing the ledger store, ingestion, faults or evals.
"""

from typing import Any

from meridian.agents.base import Agent, AgentConfig
from meridian.agents.portfolio.pipeline_intelligence import PipelineIntelligence
from meridian.agents.portfolio.safety_signal_scout import SafetySignalScout
from meridian.agents.portfolio.stage_gate_pack import StageGatePack
from meridian.agents.preclinical.dose_translation import DoseTranslation
from meridian.agents.preclinical.ind_readiness import IndReadiness
from meridian.agents.preclinical.tox_study_monitor import ToxStudyMonitor
from meridian.agents.preclinical.translational_bridge import TranslationalBridge
from meridian.agents.quality.audit_trail_reviewer import AuditTrailReviewer
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
        (AuditTrailReviewer(), cfg.audit_trail_reviewer),
        (ToxStudyMonitor(), cfg.tox_study_monitor),
        (DoseTranslation(), cfg.dose_translation),
        (IndReadiness(), cfg.ind_readiness),
        (TranslationalBridge(), cfg.translational_bridge),
        (SafetySignalScout(), cfg.safety_signal_scout),
        (PipelineIntelligence(), cfg.pipeline_intelligence),
        (StageGatePack(), cfg.stage_gate_pack),
    ]
    unknown = sorted(set(cfg.enabled) - {agent.agent_id for agent, _ in known})
    if unknown:
        raise RegistrationError(f"enabled agents not known: {unknown}")
    registry = AgentRegistry()
    for agent, config in known:
        registry.register(agent, config, enabled=agent.agent_id in cfg.enabled)
    return registry
