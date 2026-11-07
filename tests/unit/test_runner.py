from collections.abc import Iterator, Sequence
from pathlib import Path

import polars as pl
import pytest
import structlog

from meridian.agents.base import (
    AgentConfig,
    AgentContext,
    FindingDraft,
    Zone,
    make_dedup_key,
)
from meridian.agents.registry import AgentRegistry
from meridian.agents.runner import AgentRunner
from meridian.data.datacut import record_data_cut
from meridian.data.study_store import StudyReader
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import ActorSuspended, ActorWoke, Event, EventDraft, FindingRaised
from meridian.domain.findings import Evidence
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify
from meridian.platform.clock import FixedClock
from meridian.sandbox.executor import InProcessExecutor
from tests.support import T0, make_ledger

STUDY = "MRD-201"


class StubReader:
    def __init__(self, cut_id: str) -> None:
        self._cut_id = cut_id

    @property
    def cut_id(self) -> str:
        return self._cut_id

    @property
    def study_id(self) -> str:
        return STUDY

    def table(self, name: str) -> pl.DataFrame:
        raise KeyError(name)

    def tables(self) -> list[str]:
        return []


class FakeAgent:
    """Returns one finding per site in `sites`; can be told to fail N times first."""

    zone = Zone.RBQM
    subscribes_to = frozenset({EventType.DATA_CUT})
    visibility = Visibility.BLINDED
    visible_to = frozenset({Role.CLINICAL_OPS_LEAD})
    config_model = AgentConfig

    def __init__(
        self,
        agent_id: str = "rbqm.fake",
        sites: Sequence[str] = ("1001",),
        fail_times: int = 0,
        duplicate: bool = False,
        version: str = "0.0.1",
    ) -> None:
        self.agent_id = agent_id
        self.version = version
        self.sites = list(sites)
        self.fail_times = fail_times
        self.duplicate = duplicate
        self.calls = 0

    def run(self, trigger: Event, ctx: AgentContext[AgentConfig]) -> Sequence[FindingDraft]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        drafts = [
            FindingDraft(
                dedup_key=make_dedup_key(
                    agent_id=self.agent_id,
                    agent_version=self.version,
                    kind=FindingKind.KRI_BREACH,
                    study_id=STUDY,
                    site_id=site,
                    cut_id=ctx.study.cut_id,
                    check="fake",
                ),
                kind=FindingKind.KRI_BREACH,
                site_id=site,
                severity=Severity.MEDIUM,
                title=f"fake {site}",
                summary="fake finding",
                evidence=(
                    Evidence(
                        source="x", selector="y", statistic="fake:value", value=1.0, threshold=None
                    ),
                ),
                score=1.0,
            )
            for site in self.sites
        ]
        return drafts + drafts[:1] if self.duplicate else drafts


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[SqliteLedger]:
    with make_ledger(tmp_path / "l.sqlite3", FixedClock(T0)) as opened:
        yield opened


def _cut(ledger: SqliteLedger, cut_id: str, study: str = STUDY) -> Event:
    event, _ = record_data_cut(
        ledger,
        clock=FixedClock(T0),
        actor="system.ingest",
        study_id=study,
        cut_id=cut_id,
        path=Path(cut_id),
    )
    return event


def _runner(ledger: SqliteLedger, *agents: FakeAgent) -> AgentRunner:
    registry = AgentRegistry()
    for agent in agents:
        registry.register(agent, AgentConfig())
    return AgentRunner(
        ledger=ledger,
        registry=registry,
        executor=InProcessExecutor(
            registry=registry, reader_factory=StubReader, log=structlog.get_logger()
        ),
        clock=FixedClock(T0),
        study_id=STUDY,
        log=structlog.get_logger(),
    )


def _types(ledger: SqliteLedger) -> list[str]:
    return [e.event_type.value for e in ledger.read()]


def test_one_pass_wakes_runs_records_and_suspends(ledger: SqliteLedger) -> None:
    cut = _cut(ledger, "DC-A")
    report = _runner(ledger, FakeAgent(sites=["1001", "1002"])).run_pending()
    assert report.completed == [("rbqm.fake", cut.seq)]
    assert _types(ledger) == [
        "data_cut",
        "actor_woke",
        "finding_raised",
        "finding_raised",
        "actor_suspended",
    ]
    woke, suspended = ledger.read()[1], ledger.read()[-1]
    assert isinstance(woke, Event)
    assert isinstance(woke.payload, ActorWoke)
    assert isinstance(suspended, Event)
    assert isinstance(suspended.payload, ActorSuspended)
    assert woke.payload.trigger_seq == suspended.payload.trigger_seq == cut.seq
    finding_event = ledger.read()[2]
    assert isinstance(finding_event, Event)
    assert isinstance(finding_event.payload, FindingRaised)
    finding = finding_event.payload.finding
    assert finding.agent_id == "rbqm.fake"
    assert finding.agent_version == "0.0.1"
    assert finding.visible_to == frozenset({Role.CLINICAL_OPS_LEAD})
    assert finding.finding_id == "RBQM-0001"
    assert verify(ledger).ok


def test_two_passes_are_idempotent(ledger: SqliteLedger) -> None:
    _cut(ledger, "DC-A")
    _cut(ledger, "DC-B")
    runner = _runner(ledger, FakeAgent(), FakeAgent(agent_id="rbqm.other"))
    runner.run_pending()
    length = len(ledger.read())
    second = runner.run_pending()
    assert len(ledger.read()) == length
    assert second.completed == []
    assert second.findings_appended == []


def test_failing_agent_is_retried_next_pass_and_findings_appended_once(
    ledger: SqliteLedger,
) -> None:
    cut = _cut(ledger, "DC-A")
    agent = FakeAgent(sites=["1001", "1002"], fail_times=1)
    runner = _runner(ledger, agent)
    first = runner.run_pending()
    assert first.failed == [("rbqm.fake", cut.seq, "RuntimeError: boom")]
    assert _types(ledger) == ["data_cut", "actor_woke"]
    second = runner.run_pending()
    assert second.completed == [("rbqm.fake", cut.seq)]
    third = runner.run_pending()
    assert third.completed == []
    findings = ledger.read(event_types=[EventType.FINDING_RAISED])
    assert len(findings) == 2
    assert agent.calls == 2
    assert _types(ledger).count("actor_woke") == 2
    assert _types(ledger).count("actor_suspended") == 1


def test_dedup_key_prevents_a_duplicate_finding(ledger: SqliteLedger) -> None:
    _cut(ledger, "DC-A")
    report = _runner(ledger, FakeAgent(sites=["1001"], duplicate=True)).run_pending()
    assert len(report.findings_appended) == 1
    assert report.findings_skipped == report.findings_appended
    assert len(ledger.read(event_types=[EventType.FINDING_RAISED])) == 1


def test_crash_after_findings_reruns_without_duplicates(ledger: SqliteLedger) -> None:
    """Simulate a crash between the findings and ActorSuspended: dedup absorbs the rerun."""
    _cut(ledger, "DC-A")
    agent = FakeAgent(sites=["1001", "1002"])
    runner = _runner(ledger, agent)
    original = runner._append

    def crash_on_suspend(actor: str, visibility: Visibility, payload: object) -> Event:
        if isinstance(payload, ActorSuspended):
            raise KeyboardInterrupt
        return original(actor, visibility, payload)  # type: ignore[arg-type]

    runner._append = crash_on_suspend  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        runner.run_pending()
    runner._append = original  # type: ignore[method-assign]
    report = runner.run_pending()
    assert report.findings_skipped
    assert not report.findings_appended
    assert len(ledger.read(event_types=[EventType.FINDING_RAISED])) == 2


def test_other_studies_and_unsubscribed_agents_are_ignored(ledger: SqliteLedger) -> None:
    _cut(ledger, "CUT-0001", study="MER-101")
    quiet = FakeAgent(agent_id="rbqm.quiet")
    quiet.subscribes_to = frozenset({EventType.DOCUMENT_FILED})
    report = _runner(ledger, FakeAgent(), quiet).run_pending()
    assert report.completed == []
    assert _types(ledger) == ["data_cut"]


def test_draft_with_foreign_dedup_key_fails_the_run(ledger: SqliteLedger) -> None:
    _cut(ledger, "DC-A")
    impostor = FakeAgent(agent_id="rbqm.impostor")
    impostor.agent_id = "rbqm.impostor"
    original_run = impostor.run

    def run(trigger: Event, ctx: AgentContext[AgentConfig]) -> Sequence[FindingDraft]:
        return [
            d.model_copy(
                update={"dedup_key": d.dedup_key.replace("rbqm.impostor", "rbqm.kri_monitor")}
            )
            for d in original_run(trigger, ctx)
        ]

    impostor.run = run  # type: ignore[method-assign]
    report = _runner(ledger, impostor).run_pending()
    assert report.failed
    assert ledger.read(event_types=[EventType.FINDING_RAISED]) == []


def test_new_version_runs_on_cuts_the_old_version_completed(ledger: SqliteLedger) -> None:
    cut = _cut(ledger, "DC-A")
    _runner(ledger, FakeAgent(version="1.0.0")).run_pending()
    report = _runner(ledger, FakeAgent(version="2.0.0")).run_pending()
    assert report.completed == [("rbqm.fake", cut.seq)]
    assert report.findings_skipped == []
    findings = ledger.read(event_types=[EventType.FINDING_RAISED])
    versions = sorted(
        e.payload.finding.agent_version
        for e in findings
        if isinstance(e, Event) and isinstance(e.payload, FindingRaised)
    )
    assert versions == ["1.0.0", "2.0.0"]
    suspended = [
        e.payload.agent_version
        for e in ledger.read(event_types=[EventType.ACTOR_SUSPENDED])
        if isinstance(e, Event) and isinstance(e.payload, ActorSuspended)
    ]
    assert suspended == ["1.0.0", "2.0.0"]
    # And v2 is now complete too.
    assert _runner(ledger, FakeAgent(version="2.0.0")).run_pending().completed == []


def test_legacy_suspended_without_version_does_not_complete_a_versioned_agent(
    ledger: SqliteLedger,
) -> None:
    cut = _cut(ledger, "DC-A")
    ledger.append(
        EventDraft(
            occurred_at=T0,
            actor="rbqm.fake",
            study_id=STUDY,
            visibility=Visibility.BLINDED,
            payload=ActorSuspended(agent_id="rbqm.fake", trigger_seq=cut.seq),
        )
    )
    assert _runner(ledger, FakeAgent()).run_pending().completed == [("rbqm.fake", cut.seq)]


def test_stub_reader_satisfies_protocol() -> None:
    reader: StudyReader = StubReader("X")
    assert reader.cut_id == "X"


def test_a_draft_may_narrow_visibility_but_never_widen_it(ledger: SqliteLedger) -> None:
    _cut(ledger, "DC-A")
    agent = FakeAgent(agent_id="rbqm.scoped")
    original_run = agent.run

    def narrow(trigger: Event, ctx: AgentContext[AgentConfig]) -> Sequence[FindingDraft]:
        return [
            d.model_copy(update={"visible_to": (Role.CLINICAL_OPS_LEAD,)})
            for d in original_run(trigger, ctx)
        ]

    agent.run = narrow  # type: ignore[method-assign]
    assert not _runner(ledger, agent).run_pending().failed
    (raised,) = ledger.read(event_types=[EventType.FINDING_RAISED])
    assert isinstance(raised, Event)
    assert isinstance(raised.payload, FindingRaised)
    assert raised.payload.finding.visible_to == frozenset({Role.CLINICAL_OPS_LEAD})


def test_a_draft_naming_a_role_outside_the_agent_fails_the_run_unrecorded(
    ledger: SqliteLedger,
) -> None:
    _cut(ledger, "DC-A")
    agent = FakeAgent(agent_id="rbqm.wide", sites=("1001", "1002"))
    original_run = agent.run

    def widen(trigger: Event, ctx: AgentContext[AgentConfig]) -> Sequence[FindingDraft]:
        drafts = list(original_run(trigger, ctx))
        return [drafts[0], drafts[1].model_copy(update={"visible_to": (Role.PORTFOLIO_LEAD,)})]

    agent.run = widen  # type: ignore[method-assign]
    report = _runner(ledger, agent).run_pending()
    assert report.failed
    assert "portfolio_lead" in report.failed[0][2]
    assert ledger.read(event_types=[EventType.FINDING_RAISED]) == []  # not even the first


def test_run_pending_can_be_limited_to_agents_and_cuts(ledger: SqliteLedger) -> None:
    _cut(ledger, "DC-A")
    _cut(ledger, "DC-B")
    a, b = FakeAgent(agent_id="rbqm.a"), FakeAgent(agent_id="rbqm.b")
    report = _runner(ledger, a, b).run_pending(agents={"rbqm.a"}, cuts={"DC-B"})
    assert report.completed == [("rbqm.a", 2)]
    rest = _runner(ledger, a, b).run_pending()
    assert sorted(rest.completed) == [("rbqm.a", 1), ("rbqm.b", 1), ("rbqm.b", 2)]
