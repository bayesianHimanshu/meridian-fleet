"""Shared identity and helpers for RBQM agents. Blinded data only."""

from meridian.agents.base import Zone
from meridian.data.study_store import StudyReader
from meridian.domain.enums import EventType, Role, Visibility
from meridian.domain.events import DataCut, Event

RBQM_VISIBLE_TO = frozenset({Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.QA_AUDITOR})
RBQM_VERSION = "2.0.0"


class RbqmAgentBase:
    """Identity shared by the RBQM agents. Subclasses set agent_id and config_model."""

    version = RBQM_VERSION
    zone = Zone.RBQM
    subscribes_to = frozenset({EventType.DATA_CUT})
    visibility = Visibility.BLINDED
    visible_to = RBQM_VISIBLE_TO


def cut_id_of(trigger: Event, study: StudyReader) -> str:
    if isinstance(trigger.payload, DataCut) and trigger.payload.cut_id != study.cut_id:
        raise ValueError(
            f"trigger is for cut {trigger.payload.cut_id} but the reader is for {study.cut_id}"
        )
    return study.cut_id
