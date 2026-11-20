"""Shared builders for the preclinical tests: a synthetic study written as a SEND cut, its
SendStudyLoaded trigger, and the agent run on it in process."""

import uuid
from pathlib import Path

import structlog

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.preclinical.tox_study_monitor import ToxStudyMonitor
from meridian.data.send import write_send_cut
from meridian.data.study_store import SendStudyReader
from meridian.domain.enums import Visibility
from meridian.domain.events import Event, SendStudyLoaded
from meridian.platform.agent_settings import ToxStudyMonitorConfig
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.preclinical.synth import STUDY, StudySpec, build
from tests.support import T0

CUT = "TOX-SYN"


def send_trigger(study: str = STUDY, cut: str = CUT, seq: int = 5) -> Event:
    return Event(
        seq=seq,
        event_id=uuid.UUID("01890000-0000-7000-8000-000000000005"),
        occurred_at=T0,
        recorded_at=T0,
        actor="system.ingest",
        study_id=study,
        visibility=Visibility.BLINDED,
        payload=SendStudyLoaded(study_id=study, cut_id=cut, path=f"send/{study}/{cut}"),
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def write_study(root: Path, spec: StudySpec, cut: str = CUT) -> SendStudyReader:
    write_send_cut(
        send_root=root, study_id=STUDY, cut_id=cut, tables=build(spec), created_at=T0, sources=()
    )
    return SendStudyReader(root / STUDY, cut)


def config() -> ToxStudyMonitorConfig:
    return Settings().agents.tox_study_monitor


def run(
    root: Path, spec: StudySpec, cfg: ToxStudyMonitorConfig | None = None
) -> list[FindingDraft]:
    reader = write_study(root, spec)
    ctx = AgentContext(
        study=reader, clock=FixedClock(T0), config=cfg or config(), log=structlog.get_logger()
    )
    return list(ToxStudyMonitor().run(send_trigger(), ctx))


def checks(drafts: list[FindingDraft]) -> list[str]:
    return sorted(d.dedup_key.rsplit("|", 1)[1] for d in drafts)
