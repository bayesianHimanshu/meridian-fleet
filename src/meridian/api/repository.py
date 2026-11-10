"""Read and decide, as one caller: every endpoint gets its data here, never from the ledger.

A Repository is built per request for one person. It reads the ledger with the caller's
role (so restricted events come back redacted, the ledger's own rule) and passes every
finding, narrative and decision through meridian.api.visibility. Nothing here updates or
deletes: a decision is one appended HumanDecision event, written with a guarded append so
that two concurrent decisions on one finding cannot both pass the rules.
"""

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from meridian.agents.base import zone_of
from meridian.agents.quality import audit_trail_reviewer, tmf_completeness
from meridian.agents.rbqm import narrator
from meridian.agents.rbqm.describe import describe
from meridian.agents.registry import AgentRegistry
from meridian.agents.safety import local_narrator, term_adjudicator
from meridian.api import safety as safety_view
from meridian.api import tmf as tmf_view
from meridian.api import visibility
from meridian.api.identity import Person
from meridian.api.models import (
    AnswerView,
    AssessmentView,
    AssetLive,
    AssetStatic,
    AuditFindingItem,
    AuditView,
    CheckCount,
    ContainmentCheckView,
    ContainmentView,
    CutKind,
    CutScope,
    CutSummary,
    DecisionResponse,
    DecisionView,
    Enrollment,
    FindingDetail,
    FindingPage,
    FindingStatus,
    FindingSummary,
    Fleet,
    FleetAgent,
    LatestNarrative,
    LedgerPage,
    NarrativeView,
    ObservationItem,
    ObservationRequest,
    ObservationResponse,
    Portfolio,
    PortfolioAsset,
    RbqmSites,
    Reconciliation,
    ReviewQueue,
    RunEvent,
    RunTrace,
    SiteFlag,
    StudySummary,
    TmfCompletenessView,
    TmfFindingItem,
    UnblindedCases,
    VerifyReport,
)
from meridian.api.sites import site_metrics
from meridian.api.study import enrollment
from meridian.data.study_store import MissingTableError, StudyAccessError, StudyReader
from meridian.domain import audit
from meridian.domain.audit import LEDGER_SCOPE
from meridian.domain.dedup import make_dedup_key, parse_dedup_key
from meridian.domain.enums import DecisionAction, EventType, FindingKind, Role, Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    ContainmentCheckCompleted,
    DataCut,
    Event,
    EventDraft,
    FindingAssessment,
    FindingNarrative,
    FindingRaised,
    HumanDecision,
    LedgerEntry,
    QualityObservation,
)
from meridian.domain.findings import Decision, Evidence, Finding
from meridian.ledger.store import LedgerReader, LedgerWriter, RawLedgerSource
from meridian.ledger.verify import verify
from meridian.platform.agent_settings import AuditTrailReviewerConfig, TmfCompletenessConfig
from meridian.platform.clock import Clock
from meridian.platform.config import AgentsSettings
from meridian.sandbox.policies import (
    ALL_POLICIES,
    LOCAL_MODEL_POLICY,
    NARRATOR_POLICY,
    applied_sha256,
    policy_for_agent,
    policy_path,
)

OPAQUE_CUT = re.compile(r"^DC-[0-9a-f]{8}$")
CLOSED: frozenset[DecisionAction] = frozenset({DecisionAction.ACCEPTED, DecisionAction.DISMISSED})
LEDGER_PAGE_MAX = 1000
RUN_WINDOW = timedelta(days=7)  # the fleet page's "last 7 days"


class NotFoundError(Exception):
    """The thing does not exist, or the caller may not see it (never told apart)."""


class ForbiddenError(Exception):
    pass


class ConflictError(Exception):
    pass


class InvalidDecisionError(Exception):
    pass


class Ledger(LedgerReader, LedgerWriter, RawLedgerSource, Protocol):
    def append_guarded(self, draft: EventDraft, guard: Callable[[], None]) -> Event: ...

    def append_many_guarded(
        self, drafts: Sequence[EventDraft], guard: Callable[[], None]
    ) -> list[Event]: ...


@dataclass(frozen=True, slots=True)
class _Raised:
    event: Event
    finding: Finding
    finding_id: str
    cut_id: str
    check: str
    dedup_key: str


@dataclass(slots=True)
class Snapshot:
    """The ledger as one role sees it, indexed."""

    role: Role
    cuts: dict[int, DataCut] = field(default_factory=dict)
    cut_events: dict[int, Event] = field(default_factory=dict)
    raised: dict[str, _Raised] = field(default_factory=dict)
    narratives: dict[str, list[Event]] = field(default_factory=lambda: defaultdict(list))
    # Under each finding it names; only when the role may see every one of them.
    assessments: dict[str, list[Event]] = field(default_factory=lambda: defaultdict(list))
    decisions: dict[str, list[Event]] = field(default_factory=lambda: defaultdict(list))
    woke: list[Event] = field(default_factory=list)
    suspended: list[Event] = field(default_factory=list)
    containment: list[Event] = field(default_factory=list)
    # finding id -> id of the newest-version finding with the same (agent, cut, kind, site,
    # check), for every finding a newer version superseded.
    superseded_by: dict[str, str] = field(default_factory=dict)
    # finding id -> the newer agent version that completed a run on the finding's cut
    # and raised nothing with the same (kind, site, check).
    retired_by: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, entries: Sequence[LedgerEntry], role: Role) -> Snapshot:
        snap = cls(role=role)
        narratives: list[Event] = []
        assessments: list[Event] = []
        decisions: list[Event] = []
        for entry in entries:
            shown = visibility.redact(role, entry)
            if not isinstance(shown, Event):
                continue
            p = shown.payload
            if isinstance(p, DataCut):
                snap.cuts[shown.seq] = p
                snap.cut_events[shown.seq] = shown
            elif isinstance(p, FindingRaised):
                if p.finding.finding_id is not None and visibility.can_see_finding(role, p.finding):
                    parts = parse_dedup_key(p.dedup_key)
                    snap.raised[p.finding.finding_id] = _Raised(
                        shown,
                        p.finding,
                        p.finding.finding_id,
                        parts.cut_id,
                        parts.check,
                        p.dedup_key,
                    )
            elif isinstance(p, FindingNarrative):
                narratives.append(shown)
            elif isinstance(p, FindingAssessment):
                assessments.append(shown)
            elif isinstance(p, HumanDecision):
                decisions.append(shown)
            elif isinstance(p, ActorWoke):
                snap.woke.append(shown)
            elif isinstance(p, ActorSuspended):
                snap.suspended.append(shown)
            elif isinstance(p, ContainmentCheckCompleted):
                snap.containment.append(shown)
        # A narrative or a decision is visible exactly when its finding is.
        for e in narratives:
            assert isinstance(e.payload, FindingNarrative)
            if e.payload.finding_id in snap.raised:
                snap.narratives[e.payload.finding_id].append(e)
        for e in assessments:
            assert isinstance(e.payload, FindingAssessment)
            if visibility.can_see_assessment(e.payload, snap.raised):
                for finding_id in e.payload.finding_ids:
                    snap.assessments[finding_id].append(e)
        for e in decisions:
            assert isinstance(e.payload, HumanDecision)
            if e.payload.decision.finding_id in snap.raised:
                snap.decisions[e.payload.decision.finding_id].append(e)
        snap.superseded_by = _supersession(snap.raised.values())
        snap.retired_by = _retirement(snap)
        return snap

    def decided(self, finding_id: str) -> FindingStatus:
        history = self.decisions.get(finding_id, [])
        if not history:
            return "open"
        last = history[-1].payload
        assert isinstance(last, HumanDecision)
        status: FindingStatus = last.decision.action.value
        return status

    def status(self, finding_id: str) -> FindingStatus:
        """The latest decision; a retired finding nobody closed reads retired."""
        decided = self.decided(finding_id)
        if finding_id in self.retired_by and decided in ("open", "escalated"):
            return "retired"
        return decided

    def current(self, finding_id: str) -> bool:
        """Newest version's finding, still raised by the newest run: neither superseded
        nor retired."""
        return finding_id not in self.superseded_by and finding_id not in self.retired_by


def _decision_view(e: Event) -> DecisionView:
    assert isinstance(e.payload, HumanDecision)
    d = e.payload.decision
    return DecisionView(
        seq=e.seq,
        hash=e.hash,
        prev_hash=e.prev_hash,
        recorded_at=e.recorded_at,
        actor=d.actor,
        role=d.role,
        action=d.action,
        reason=d.reason,
    )


def _latest_narrative(events: Sequence[Event]) -> LatestNarrative | None:
    for e in reversed(events):
        n = e.payload
        assert isinstance(n, FindingNarrative)
        if n.status == "accepted" and n.headline and n.explanation:
            return LatestNarrative(
                narrator_version=n.agent_version,
                headline=n.headline,
                explanation=n.explanation,
                seq=e.seq,
                narrator_id=n.agent_id,
                model=n.model,
            )
    return None


def _run_event(e: Event | None) -> RunEvent | None:
    if e is None:
        return None
    p = e.payload
    if isinstance(p, ActorSuspended):
        return RunEvent(
            seq=e.seq,
            hash=e.hash,
            recorded_at=e.recorded_at,
            executor=p.executor,
            image_digest=p.image_digest,
            applied_policy_sha256=p.policy_sha256,
            policy_file=p.policy_file,
            policy_file_sha256=p.policy_file_sha256,
            inputs_manifest_sha256=p.inputs_manifest_sha256,
        )
    assert isinstance(p, ActorWoke)
    return RunEvent(seq=e.seq, hash=e.hash, recorded_at=e.recorded_at, executor=p.executor)


@dataclass(frozen=True, slots=True)
class _Run:
    woke: Event
    suspended: Event | None  # None: the run never finished (it failed or was stopped)


def _runs(woke: Iterable[Event], suspended: Iterable[Event]) -> list[_Run]:
    """Each ActorWoke with the ActorSuspended that closed it: the first one for the same
    (agent, version, trigger) after it and before that key's next ActorWoke. A retry's
    ActorSuspended therefore never closes the failed attempt before it."""

    def key(e: Event) -> tuple[str, str | None, int]:
        p = e.payload
        assert isinstance(p, ActorWoke | ActorSuspended)
        return (p.agent_id, p.agent_version, p.trigger_seq)

    events = sorted([*woke, *suspended], key=lambda e: e.seq)
    open_runs: dict[tuple[str, str | None, int], int] = {}
    runs: list[_Run] = []
    for e in events:
        if isinstance(e.payload, ActorWoke):
            open_runs[key(e)] = len(runs)
            runs.append(_Run(e, None))
        elif isinstance(e.payload, ActorSuspended):
            index = open_runs.pop(key(e), None)
            if index is not None:
                runs[index] = _Run(runs[index].woke, e)
    return runs


def _version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in v.split("."))
    except ValueError:
        return ()


def _supersession(raised: Iterable[_Raised]) -> dict[str, str]:
    """Among findings of one agent with the same (cut, kind, site, check), every one but
    the newest agent version is superseded by the newest. Computed over what the caller
    may see only."""
    groups: dict[tuple[str, str, str, str, str], list[_Raised]] = defaultdict(list)
    for r in raised:
        f = r.finding
        groups[(f.agent_id, r.cut_id, f.kind.value, f.site_id or "STUDY", r.check)].append(r)
    out: dict[str, str] = {}
    for members in groups.values():
        newest = max(members, key=lambda r: (_version(r.finding.agent_version), r.event.seq))
        for r in members:
            if _version(r.finding.agent_version) < _version(newest.finding.agent_version):
                out[r.finding_id] = newest.finding_id
    return out


def _retirement(snap: Snapshot) -> dict[str, str]:
    """For each finding a newer version did not supersede: the newest newer version of the
    same agent that completed a run on the same cut (an ActorSuspended for that cut's
    DataCut) without raising the same (kind, site, check)."""
    cut_of = {seq: cut.cut_id for seq, cut in snap.cuts.items()}
    completed: dict[tuple[str, str], set[str]] = defaultdict(set)  # (agent, cut) -> versions
    for e in snap.suspended:
        p = e.payload
        if isinstance(p, ActorSuspended) and p.agent_version and p.trigger_seq in cut_of:
            completed[(p.agent_id, cut_of[p.trigger_seq])].add(p.agent_version)
    raised_keys: dict[tuple[str, str, str], set[tuple[str, str, str]]] = defaultdict(set)
    for r in snap.raised.values():
        f = r.finding
        raised_keys[(f.agent_id, f.agent_version, r.cut_id)].add(
            (f.kind.value, f.site_id or "STUDY", r.check)
        )
    out: dict[str, str] = {}
    for r in snap.raised.values():
        if r.finding_id in snap.superseded_by:
            continue
        f = r.finding
        key = (f.kind.value, f.site_id or "STUDY", r.check)
        own = _version(f.agent_version)
        retiring = [
            v
            for v in completed.get((f.agent_id, r.cut_id), set())
            if _version(v) > own and key not in raised_keys[(f.agent_id, v, r.cut_id)]
        ]
        if retiring:
            out[r.finding_id] = max(retiring, key=_version)
    return out


RECONCILER = "safety.sae_reconciler"


def cut_kind(cut_id: str, label: str | None) -> CutKind:
    name = cut_id.removeprefix("DC-")
    if name == "BASE":
        return "base"
    if name.startswith("NULL"):
        return "null"
    if name.startswith("SWEEP"):
        return "sweep"
    if "HOLDOUT" in name or (label is not None and "holdout" in label.lower()):
        return "holdout"
    if label is not None and label.startswith(("safety_clean_", "tmf_clean_")):
        return "null"  # seeded clean safety and TMF cuts: nothing planted
    return "faulted"


@dataclass(frozen=True, slots=True)
class PortfolioEntry:
    asset_id: str
    static: AssetStatic
    live_study: bool


ScopeFilter = Literal["operational", "evaluation", "all"]


@dataclass(frozen=True, slots=True)
class FindingFilters:
    """Defaults: operational cuts only, superseded findings left out."""

    scope: ScopeFilter = "operational"
    include_superseded: bool = False
    include_retired: bool = False
    cut: str | None = None
    agent: str | None = None
    kind: str | None = None
    site: str | None = None
    severity: str | None = None
    status: str | None = None


class Repository:
    def __init__(
        self,
        *,
        ledger: Ledger,
        person: Person,
        clock: Clock,
        study_id: str,
        registry: AgentRegistry,
        policies_dir: Path,
        ground_truth_dir: Path,
        portfolio: Sequence[PortfolioEntry],
        operational_cuts: Collection[str] = ("DC-BASE",),
        run_timeout_seconds: int = 120,
    ) -> None:
        # A started detector run without its ActorSuspended counts as running, not failed,
        # until this many seconds after its ActorWoke ([runtime] exec_timeout_seconds).
        self._run_timeout = timedelta(seconds=run_timeout_seconds)
        self._ledger = ledger
        self.person = person
        self.role = person.role
        self._clock = clock
        self._study_id = study_id
        self._registry = registry
        self._policies_dir = policies_dir
        self._ground_truth_dir = ground_truth_dir
        self._portfolio = portfolio
        self._operational = frozenset(operational_cuts)
        self._snapshot: Snapshot | None = None

    def scope(self, cut_id: str) -> CutScope:
        """A data cut's scope. Findings about the ledger itself (audit findings, QA
        observations; cut "LEDGER") are about the live study: operational."""
        if cut_id == LEDGER_SCOPE:
            return "operational"
        return "operational" if cut_id in self._operational else "evaluation"

    # ---- reading

    def snapshot(self) -> Snapshot:
        if self._snapshot is None:
            entries = self._ledger.read(viewer_role=visibility.ledger_viewer(self.role))
            self._snapshot = Snapshot.build(entries, self.role)
        return self._snapshot

    def _summary(self, r: _Raised) -> FindingSummary:
        snap = self.snapshot()
        f = r.finding
        return FindingSummary(
            finding_id=r.finding_id,
            seq=r.event.seq,
            raised_at=r.event.recorded_at,
            study_id=f.study_id,
            cut_id=r.cut_id,
            scope=self.scope(r.cut_id),
            check=r.check,
            agent_id=f.agent_id,
            agent_version=f.agent_version,
            superseded=r.finding_id in snap.superseded_by,
            superseded_by=snap.superseded_by.get(r.finding_id),
            retired=r.finding_id in snap.retired_by,
            retired_by_version=snap.retired_by.get(r.finding_id),
            primary_evidence=[
                e.display for e in describe(f.evidence) if e.role == "primary" and e.display
            ],
            kind=f.kind,
            site_id=f.site_id,
            severity=f.severity,
            title=f.title,
            summary=f.summary,
            score=f.score,
            visibility=f.visibility,
            visible_to=sorted(f.visible_to),
            status=snap.status(r.finding_id),
            decision_count=len(snap.decisions.get(r.finding_id, [])),
            latest_narrative=_latest_narrative(snap.narratives.get(r.finding_id, [])),
        )

    def studies(self) -> list[StudySummary]:
        counts: dict[str, int] = defaultdict(int)
        for cut in self.snapshot().cuts.values():
            counts[cut.study_id] += 1
        return [StudySummary(study_id=s, cuts=counts[s]) for s in sorted(counts)]

    def _label(self, cut_id: str) -> str | None:
        """Only the label field of a ground truth file is ever read here."""
        path = self._ground_truth_dir / f"{cut_id}.json"
        try:
            label = json.loads(path.read_text(encoding="utf-8")).get("label")
        except OSError, ValueError:
            return None
        return label if isinstance(label, str) else None

    def cuts(self, study_id: str) -> list[CutSummary]:
        snap = self.snapshot()
        cuts = {seq: c for seq, c in snap.cuts.items() if c.study_id == study_id}
        if not cuts:
            raise NotFoundError(f"study {study_id} not found")
        completed: dict[int, int] = defaultdict(int)
        for e in snap.suspended:
            assert isinstance(e.payload, ActorSuspended)
            completed[e.payload.trigger_seq] += 1
        out = []
        for seq, cut in sorted(cuts.items()):
            opaque = bool(OPAQUE_CUT.match(cut.cut_id))
            show = not opaque or visibility.can_see_cut_label(self.role)
            label = self._label(cut.cut_id) if show and opaque else None
            out.append(
                CutSummary(
                    cut_id=cut.cut_id,
                    trigger_seq=seq,
                    kind=cut_kind(cut.cut_id, label) if show else None,
                    label=label,
                    opaque=opaque,
                    scope=self.scope(cut.cut_id),
                    created=snap.cut_events[seq].occurred_at,
                    agent_runs_completed=completed[seq],
                )
            )
        return out

    def _filtered(self, filters: FindingFilters) -> list[_Raised]:
        snap = self.snapshot()
        out = []
        for r in snap.raised.values():
            f = r.finding
            if filters.scope != "all" and self.scope(r.cut_id) != filters.scope:
                continue
            if not filters.include_superseded and r.finding_id in snap.superseded_by:
                continue
            if not filters.include_retired and r.finding_id in snap.retired_by:
                continue
            if filters.cut is not None and r.cut_id != filters.cut:
                continue
            if filters.agent is not None and f.agent_id != filters.agent:
                continue
            if filters.kind is not None and f.kind.value != filters.kind:
                continue
            if filters.site is not None and (f.site_id or "STUDY") != filters.site:
                continue
            if filters.severity is not None and f.severity.value != filters.severity:
                continue
            if filters.status is not None and snap.status(r.finding_id) != filters.status:
                continue
            out.append(r)
        return sorted(out, key=lambda r: r.event.seq, reverse=True)  # newest first

    def findings(self, filters: FindingFilters, limit: int, offset: int) -> FindingPage:
        matched = self._filtered(filters)
        page = matched[offset : offset + limit]
        return FindingPage(
            items=[self._summary(r) for r in page], total=len(matched), limit=limit, offset=offset
        )

    def _visible(self, finding_id: str) -> _Raised:
        r = self.snapshot().raised.get(finding_id)
        if r is None:
            raise NotFoundError("finding not found")
        return r

    def finding(self, finding_id: str) -> FindingDetail:
        r = self._visible(finding_id)
        snap = self.snapshot()
        narratives = [
            NarrativeView(
                seq=e.seq,
                recorded_at=e.recorded_at,
                narrator_version=p.agent_version,
                status=p.status,
                headline=p.headline,
                explanation=p.explanation,
                model=p.model,
                rejection_reasons=list(p.rejection_reasons),
                executor=p.executor,
                sandbox=p.sandbox,
                narrator_id=p.agent_id,
                model_digest=p.model_digest,
            )
            for e in snap.narratives.get(finding_id, [])
            if isinstance((p := e.payload), FindingNarrative)
        ]
        assessments = [
            AssessmentView(
                seq=e.seq,
                recorded_at=e.recorded_at,
                agent_id=a.agent_id,
                agent_version=a.agent_version,
                candidate=a.candidate,
                finding_ids=list(a.finding_ids),
                status=a.status,
                verdict=a.verdict,
                reason=a.reason,
                model=a.model,
                model_digest=a.model_digest,
                rejection_reasons=list(a.rejection_reasons),
                error=a.error,
                executor=a.executor,
                sandbox=a.sandbox,
                answers=[
                    AnswerView(
                        model=x.model,
                        model_digest=x.model_digest,
                        status=x.status,
                        verdict=x.verdict,
                        reason=x.reason,
                        text=x.text,
                        rejection_reasons=list(x.rejection_reasons),
                        error=x.error,
                        latency_ms=x.latency_ms,
                    )
                    for x in a.answers
                ],
            )
            for e in snap.assessments.get(finding_id, [])
            if isinstance((a := e.payload), FindingAssessment)
        ]
        return FindingDetail(
            finding=self._summary(r),
            dedup_key=r.dedup_key,
            hash=r.event.hash,
            evidence=list(r.finding.evidence),
            narratives=narratives,
            assessments=assessments,
            decisions=[_decision_view(e) for e in snap.decisions.get(finding_id, [])],
            run=self._run(r),
        )

    def _run(self, r: _Raised) -> RunTrace | None:
        """The ActorWoke before the finding and the ActorSuspended after it, for the same
        agent, version and DataCut: the runner writes woke, findings, suspended in order."""
        snap = self.snapshot()
        trigger = next((seq for seq, c in snap.cuts.items() if c.cut_id == r.cut_id), None)
        if trigger is None:
            return None
        f = r.finding

        def same(p: ActorWoke | ActorSuspended) -> bool:
            return (p.agent_id, p.agent_version, p.trigger_seq) == (
                f.agent_id,
                f.agent_version,
                trigger,
            )

        woke = [e for e in snap.woke if isinstance(e.payload, ActorWoke) and same(e.payload)]
        suspended = [
            e for e in snap.suspended if isinstance(e.payload, ActorSuspended) and same(e.payload)
        ]
        before = [e for e in woke if e.seq < r.event.seq]
        after = [e for e in suspended if e.seq > r.event.seq]
        return RunTrace(
            agent_id=f.agent_id,
            agent_version=f.agent_version,
            trigger_seq=trigger,
            cut_id=r.cut_id,
            woke=_run_event(before[-1] if before else None),
            suspended=_run_event(after[0] if after else None),
        )

    def review_queue(self, filters: FindingFilters | None = None) -> ReviewQueue:
        """Open and escalated findings, newest first; by default operational cuts only and
        no superseded findings, so evaluation cuts never reach it unless asked for."""
        snap = self.snapshot()
        items = [
            self._summary(r)
            for r in self._filtered(filters or FindingFilters())
            if snap.status(r.finding_id) in ("open", "escalated")
        ]
        return ReviewQueue(items=items)

    def ledger_page(
        self,
        since: int,
        types: Collection[EventType] | None,
        limit: int,
        order: Literal["asc", "desc"] = "asc",
        before: int | None = None,
        agent: str | None = None,
    ) -> LedgerPage:
        """asc: events after since, oldest first. desc: the newest events before `before`
        (or the newest of all), newest first, for a reader paging back in time. agent:
        only events shown in full whose payload names it; it filters after redaction, so it
        never reveals which agent a redacted event belongs to."""
        entries = self._ledger.read(
            since_seq=since, event_types=types, viewer_role=visibility.ledger_viewer(self.role)
        )
        visible = self.snapshot().raised.keys()

        def shown_as(e: LedgerEntry) -> LedgerEntry:
            return visibility.redact_with_findings(self.role, e, visible)

        if agent is not None:
            entries = [e for e in entries if _agent_of(shown_as(e)) == agent]

        if order == "desc":
            older = [e for e in entries if before is None or e.seq < before]
            page_desc = [shown_as(e) for e in reversed(older[-limit:])]
            more_older = len(older) > limit
            return LedgerPage(
                entries=page_desc,
                next_since=None,
                next_before=page_desc[-1].seq if more_older and page_desc else None,
            )
        shown = [shown_as(e) for e in entries[: limit + 1]]
        more = len(shown) > limit
        page = shown[:limit]
        return LedgerPage(entries=page, next_since=page[-1].seq if more and page else None)

    def verify(self) -> VerifyReport:
        report = verify(self._ledger)
        return VerifyReport(
            ok=report.ok,
            events_checked=report.events_checked,
            first_broken_seq=report.first_broken_seq,
            problem=report.problem,
        )

    def fleet(self) -> Fleet:
        snap = self.snapshot()
        now = self._clock.now()
        since = now - RUN_WINDOW
        runs = _runs(snap.woke, snap.suspended)

        def running(r: _Run) -> bool:
            return r.suspended is None and now - r.woke.recorded_at <= self._run_timeout

        def failed(r: _Run) -> bool:
            return r.suspended is None and not running(r)

        agents: list[FleetAgent] = []
        for registered in self._registry.all():
            a = registered.agent
            policy_name = policy_for_agent(a.agent_id)
            policy = policy_path(policy_name, self._policies_dir)
            mine = [r for r in runs if _woke(r).agent_id == a.agent_id]
            current = [r for r in mine if _woke(r).agent_version == a.version]
            last = current[-1] if current else None
            recent = [r for r in mine if r.woke.recorded_at >= since]
            failures = [r for r in mine if failed(r)]
            agents.append(
                FleetAgent(
                    agent_id=a.agent_id,
                    version=a.version,
                    zone=a.zone.value,
                    role="detector",
                    enabled=registered.enabled,
                    policy=policy_name,
                    policy_file=policy.as_posix(),
                    policy_file_sha256=_file_sha256(policy),
                    applied_policy_sha256=applied_sha256(policy_name),
                    last_run_at=None if last is None else last.woke.recorded_at,
                    last_outcome=None
                    if last is None
                    else "completed"
                    if last.suspended is not None
                    else "running"
                    if running(last)
                    else "not_completed",
                    last_trigger_seq=None if last is None else _trigger(last.woke),
                    runs_7d=len(recent),
                    failed_7d=sum(1 for r in recent if failed(r)),
                    last_failure_at=failures[-1].woke.recorded_at if failures else None,
                    run_event_types=[EventType.ACTOR_WOKE, EventType.ACTOR_SUSPENDED],
                )
            )
        narratives = sorted(
            {e.seq: e for es in snap.narratives.values() for e in es}.values(),
            key=lambda e: e.seq,
        )
        assessed = sorted(
            {e.seq: e for es in snap.assessments.values() for e in es}.values(),
            key=lambda e: e.seq,
        )
        rows: tuple[
            tuple[str, str, str, Literal["narrator", "adjudicator"], list[Event], EventType], ...
        ] = (
            (
                narrator.AGENT_ID,
                narrator.VERSION,
                NARRATOR_POLICY,
                "narrator",
                narratives,
                EventType.FINDING_NARRATIVE,
            ),
            (
                local_narrator.AGENT_ID,
                local_narrator.VERSION,
                LOCAL_MODEL_POLICY,
                "narrator",
                narratives,
                EventType.FINDING_NARRATIVE,
            ),
            (
                term_adjudicator.AGENT_ID,
                term_adjudicator.VERSION,
                LOCAL_MODEL_POLICY,
                "adjudicator",
                assessed,
                EventType.FINDING_ASSESSMENT,
            ),
        )
        for agent_id, version, policy_name, role, events, kind in rows:
            agents.append(
                self._model_agent(agent_id, version, policy_name, role, events, kind, since)
            )
        latest: dict[str, Event] = {}
        for e in snap.containment:
            assert isinstance(e.payload, ContainmentCheckCompleted)
            latest[e.payload.policy_name or e.payload.zone] = e
        containment = []
        for policy_name in sorted(latest):
            e = latest[policy_name]
            p = e.payload
            assert isinstance(p, ContainmentCheckCompleted)
            containment.append(
                ContainmentView(
                    policy=policy_name,
                    zone=p.zone,
                    seq=e.seq,
                    recorded_at=e.recorded_at,
                    sandbox=p.sandbox,
                    image_digest=p.image_digest,
                    applied_policy_sha256=p.policy_sha256,
                    passed=p.passed,
                    all_refused=p.all_refused,
                    log_line_found=p.log_line_found,
                    credential_value_visible=p.credential_value_visible,
                    checks=[
                        ContainmentCheckView(
                            name=c.name,
                            expect=c.expect,
                            outcome=c.outcome,
                            passed=c.passed,
                            message=c.message,
                        )
                        for c in p.checks
                    ],
                )
            )
        return Fleet(agents=sorted(agents, key=lambda a: a.agent_id), containment=containment)

    def _model_agent(
        self,
        agent_id: str,
        version: str,
        policy_name: str,
        role: Literal["narrator", "adjudicator"],
        events: Sequence[Event],
        kind: EventType,
        since: datetime,
    ) -> FleetAgent:
        """A row for an agent that calls a model: one run per recorded call outcome."""
        mine = [
            e
            for e in events
            if isinstance(e.payload, FindingNarrative | FindingAssessment)
            and e.payload.agent_id == agent_id
        ]
        current = [e for e in mine if _model_payload(e).agent_version == version]
        recent = [e for e in mine if e.recorded_at >= since]
        errors = [e for e in mine if _model_payload(e).status == "error"]
        last = current[-1] if current else None
        policy = policy_path(policy_name, self._policies_dir)
        return FleetAgent(
            agent_id=agent_id,
            version=version,
            zone=ALL_POLICIES[policy_name].zone,
            role=role,
            enabled=True,
            policy=policy_name,
            policy_file=policy.as_posix(),
            policy_file_sha256=_file_sha256(policy),
            applied_policy_sha256=applied_sha256(policy_name),
            last_run_at=None if last is None else last.recorded_at,
            last_outcome=None if last is None else _model_payload(last).status,
            last_trigger_seq=None if last is None else _first_seq(last),
            runs_7d=len(recent),
            failed_7d=sum(1 for e in recent if e in errors),
            last_failure_at=errors[-1].recorded_at if errors else None,
            run_event_types=[kind],
        )

    def flagged_checks(self, cut_id: str) -> dict[str, list[SiteFlag]]:
        """The current site-level findings on this cut (newest version, not superseded, not
        retired, any status), per site, as the caller may see them."""
        snap = self.snapshot()
        out: dict[str, list[SiteFlag]] = defaultdict(list)
        for r in sorted(snap.raised.values(), key=lambda r: r.event.seq):
            f = r.finding
            if r.cut_id != cut_id or f.site_id is None or not snap.current(r.finding_id):
                continue
            if zone_of(f.agent_id) != "rbqm":  # the heatmap shows RBQM checks only
                continue
            parameters = sorted(
                {
                    e.statistic.split(":", 1)[0].upper()
                    for e in f.evidence
                    if e.statistic.endswith((":log_sd_z", ":sd_ratio"))
                }
            )
            out[f.site_id].append(
                SiteFlag(
                    check=r.check,
                    finding_id=r.finding_id,
                    agent_id=f.agent_id,
                    agent_version=f.agent_version,
                    status=snap.status(r.finding_id),
                    parameters=parameters,
                )
            )
        return dict(out)

    def enrollment(
        self, study_id: str, cut_id: str | None, reader: Callable[[str], StudyReader]
    ) -> Enrollment:
        if not visibility.can_see_study_data(self.role):
            raise ForbiddenError(f"{self.role.value} may not see study enrollment")
        cuts = [c for c in self.snapshot().cuts.values() if c.study_id == study_id]
        if not cuts:
            raise NotFoundError(f"study {study_id} not found")
        chosen = cut_id or next(
            (c.cut_id for c in cuts if self.scope(c.cut_id) == "operational"), None
        )
        if chosen is None or not any(c.cut_id == chosen for c in cuts):
            raise NotFoundError("cut not found")
        try:
            return enrollment(reader(chosen))
        except (MissingTableError, StudyAccessError, OSError) as exc:
            raise NotFoundError("cut data not available") from exc

    def rbqm_sites(
        self,
        cut_id: str,
        reader: Callable[[str], StudyReader],
        agents: AgentsSettings,
    ) -> RbqmSites:
        if not visibility.can_see_site_metrics(self.role):
            raise ForbiddenError(f"{self.role.value} may not see RBQM site metrics")
        if not any(c.cut_id == cut_id for c in self.snapshot().cuts.values()):
            raise NotFoundError("cut not found")
        try:
            return site_metrics(reader(cut_id), agents, self.flagged_checks(cut_id))
        except (MissingTableError, StudyAccessError, OSError) as exc:
            raise NotFoundError("cut data not available") from exc

    def reconciliation(self, cut_id: str) -> Reconciliation:
        """Current safety.sae_reconciler findings on one cut, as this role may see them:
        a hidden finding is neither listed nor counted. Reads the ledger only."""
        snap = self.snapshot()
        cut = next((c for c in snap.cuts.values() if c.cut_id == cut_id), None)
        if cut is None:
            raise NotFoundError("cut not found")
        trigger = next(seq for seq, c in snap.cuts.items() if c.cut_id == cut_id)
        versions = sorted(
            {
                e.payload.agent_version
                for e in snap.suspended
                if isinstance(e.payload, ActorSuspended)
                and e.payload.agent_id == RECONCILER
                and e.payload.trigger_seq == trigger
                and e.payload.agent_version is not None
            },
            key=_version,
        )
        rows = [
            r
            for r in sorted(snap.raised.values(), key=lambda r: r.event.seq)
            if r.cut_id == cut_id
            and r.finding.agent_id == RECONCILER
            and snap.current(r.finding_id)
        ]
        items = [
            safety_view.item(r.finding_id, r.check, r.finding, snap.status(r.finding_id))
            for r in rows
        ]
        by_check, by_site = safety_view.counts(items)
        return Reconciliation(
            study_id=cut.study_id,
            cut_id=cut_id,
            scope=self.scope(cut_id),
            agent_id=RECONCILER,
            agent_version=versions[-1] if versions else None,
            run_completed=bool(versions),
            by_check=by_check,
            by_site=by_site,
            items=items,
        )

    def tmf(
        self, cut_id: str, reader: Callable[[str], StudyReader], cfg: TmfCompletenessConfig
    ) -> TmfCompletenessView:
        """The TMF of one cut: matrix and percentages from its eTMF index, and the current
        quality.tmf_completeness findings this role may see."""
        if not visibility.can_see_tmf(self.role, cfg.visible_to):
            raise ForbiddenError(f"{self.role.value} may not see the TMF")
        snap = self.snapshot()
        cut = next((c for c in snap.cuts.values() if c.cut_id == cut_id), None)
        if cut is None:
            raise NotFoundError("cut not found")
        try:
            a = tmf_completeness.assess(reader(cut_id), cfg)
        except (MissingTableError, StudyAccessError, OSError) as exc:
            raise NotFoundError("eTMF index not available for the cut") from exc
        trigger = next(seq for seq, c in snap.cuts.items() if c.cut_id == cut_id)
        versions = sorted(
            {
                e.payload.agent_version
                for e in snap.suspended
                if isinstance(e.payload, ActorSuspended)
                and e.payload.agent_id == tmf_completeness.AGENT_ID
                and e.payload.trigger_seq == trigger
                and e.payload.agent_version is not None
            },
            key=_version,
        )
        current = [
            r
            for r in sorted(snap.raised.values(), key=lambda r: r.event.seq)
            if r.cut_id == cut_id
            and r.finding.agent_id == tmf_completeness.AGENT_ID
            and snap.current(r.finding_id)
        ]
        finding_of = {r.check.partition(":")[2]: r.finding_id for r in current}
        trial, sites = tmf_view.rows(a, cfg.reference, finding_of)
        complete, timely = a.percent(None)
        trial_complete, trial_timely = a.percent("")
        return TmfCompletenessView(
            study_id=cut.study_id,
            cut_id=cut_id,
            scope=self.scope(cut_id),
            export_date=None if a.export_date is None else a.export_date.isoformat(),
            agent_id=tmf_completeness.AGENT_ID,
            agent_version=versions[-1] if versions else None,
            run_completed=bool(versions),
            completeness_percent=complete,
            timely_percent=timely,
            trial_completeness_percent=trial_complete,
            trial_timely_percent=trial_timely,
            artifacts=tmf_view.artifacts(cfg.reference),
            trial=trial,
            sites=sites,
            findings=tmf_view.items(
                [
                    TmfFindingItem(
                        finding_id=r.finding_id,
                        check=r.check.partition(":")[0],
                        site_id=r.finding.site_id,
                        severity=r.finding.severity,
                        status=snap.status(r.finding_id),
                        title=r.finding.title,
                    )
                    for r in current
                ]
            ),
        )

    def raise_observation(self, body: ObservationRequest) -> ObservationResponse:
        """A QA auditor's observation: a QualityObservation event attributed to the auditor
        and a qa_observation finding raised by that person, appended together. A finding or
        a ledger seq it refers to must exist and be visible to the auditor (else 404)."""
        if not visibility.can_raise_observation(self.role):
            raise ForbiddenError("only qa_auditor raises quality observations")
        snap = self.snapshot()
        visible_to = set(visibility.OBSERVATION_VISIBLE_TO)
        actor_under_review: str | None = None
        refers: list[Evidence] = []
        if body.finding_id is not None:
            referred = self._visible(body.finding_id)
            # Nobody may learn of a finding through an observation about it.
            visible_to &= set(referred.finding.visible_to) | {Role.QA_AUDITOR}
            refers.append(
                Evidence(
                    source="ledger",
                    selector=f"FINDING_ID={body.finding_id}",
                    statistic="observation:refers_to_finding",
                    value=float(referred.event.seq),
                    display=body.finding_id,
                )
            )
        if body.ledger_seq is not None:
            entry = self._visible_entry(body.ledger_seq, snap)
            p = entry.payload
            if isinstance(p, HumanDecision):
                actor_under_review = p.decision.actor
            for fid in visibility.finding_ids_of(entry):
                referred = snap.raised[fid]
                visible_to &= set(referred.finding.visible_to) | {Role.QA_AUDITOR}
            refers.append(
                Evidence(
                    source="ledger",
                    selector=f"SEQ={body.ledger_seq}",
                    statistic="observation:refers_to_seq",
                    value=float(body.ledger_seq),
                    display=f"ledger seq {body.ledger_seq} ({entry.event_type.value})",
                )
            )
        observation_id = "OB-" + uuid.uuid4().hex[:12]
        now = self._clock.now()
        observation = QualityObservation(
            observation_id=observation_id,
            raised_by=self.person.actor,
            title=body.title.strip(),
            description=body.description.strip(),
            severity=body.severity,
            refers_to_finding_id=body.finding_id,
            refers_to_seq=body.ledger_seq,
        )
        finding = Finding(
            kind=FindingKind.QA_OBSERVATION,
            agent_id=visibility.OBSERVATION_SOURCE,
            agent_version=visibility.OBSERVATION_VERSION,
            study_id=self._study_id,
            site_id=None,
            severity=body.severity,
            title=observation.title,
            summary=observation.description,
            evidence=(
                Evidence(
                    source="quality_observation",
                    selector=f"OBSERVATION_ID={observation_id}",
                    statistic="observation:raised_by",
                    value=0.0,
                    display=f"raised by {self.person.actor} (qa_auditor)",
                ),
                *refers,
            ),
            visibility=Visibility.BLINDED,
            visible_to=frozenset(visible_to),
            score={"high": 1.0, "medium": 0.5, "low": 0.2}[body.severity.value],
            raised_by=self.person.actor,
            actor_under_review=actor_under_review,
        )
        key = make_dedup_key(
            agent_id=finding.agent_id,
            agent_version=finding.agent_version,
            kind=finding.kind,
            study_id=self._study_id,
            site_id=None,
            cut_id=LEDGER_SCOPE,
            check=f"qa_observation:{observation_id}",
        )
        drafts = [
            EventDraft(
                occurred_at=now,
                actor=self.person.actor,
                study_id=self._study_id,
                visibility=Visibility.BLINDED,
                payload=p,
            )
            for p in (observation, FindingRaised(finding=finding, dedup_key=key))
        ]
        recorded, raised = self._ledger.append_many_guarded(drafts, lambda: None)
        self._snapshot = None
        assert isinstance(raised.payload, FindingRaised)
        new_id = raised.payload.finding.finding_id
        assert new_id is not None
        return ObservationResponse(
            observation_id=observation_id,
            observation_seq=recorded.seq,
            finding_id=new_id,
            finding_seq=raised.seq,
        )

    def _visible_entry(self, seq: int, snap: Snapshot) -> Event:
        """The ledger event at seq as this role reads it in full, or 404 (missing,
        restricted, or about a finding the role may not see: never told apart)."""
        entries = self._ledger.read(viewer_role=visibility.ledger_viewer(self.role))
        entry = next((e for e in entries if e.seq == seq), None)
        if entry is None:
            raise NotFoundError("ledger event not found")
        shown = visibility.redact_with_findings(self.role, entry, snap.raised)
        if not isinstance(shown, Event):
            raise NotFoundError("ledger event not found")
        return shown

    def observations(self) -> list[ObservationItem]:
        """The QA observations this role may see, newest first."""
        if not visibility.can_see_observations(self.role):
            raise ForbiddenError(f"{self.role.value} does not review quality observations")
        snap = self.snapshot()
        out = []
        for r in sorted(snap.raised.values(), key=lambda r: -r.event.seq):
            f = r.finding
            if f.kind is not FindingKind.QA_OBSERVATION:
                continue
            refers = {e.statistic: e.display or "" for e in f.evidence}
            out.append(
                ObservationItem(
                    finding_id=r.finding_id,
                    seq=r.event.seq,
                    recorded_at=r.event.recorded_at,
                    raised_by=f.raised_by or "",
                    severity=f.severity,
                    status=snap.status(r.finding_id),
                    title=f.title,
                    refers_to_finding_id=refers.get("observation:refers_to_finding"),
                    refers_to_seq=next(
                        (
                            int(e.value)
                            for e in f.evidence
                            if e.statistic == "observation:refers_to_seq"
                        ),
                        None,
                    ),
                )
            )
        return out

    def audit(self, cfg: AuditTrailReviewerConfig) -> AuditView:
        """The Audit tab: the last review's seq range and run, its chain linkage result, and
        the current audit findings this role may see, counted by check."""
        if not visibility.can_see_audit(self.role, cfg.visible_to):
            raise ForbiddenError(f"{self.role.value} does not review the audit trail")
        snap = self.snapshot()
        reviews = [
            e
            for e in snap.woke
            if isinstance(e.payload, ActorWoke)
            and e.payload.agent_id == audit_trail_reviewer.AGENT_ID
            and e.payload.review_to_seq is not None
        ]
        last = reviews[-1] if reviews else None
        completed = False
        if last is not None:
            assert isinstance(last.payload, ActorWoke)
            completed = any(
                isinstance(e.payload, ActorSuspended)
                and e.payload.agent_id == audit_trail_reviewer.AGENT_ID
                and e.payload.trigger_seq == last.payload.trigger_seq
                for e in snap.suspended
            )
        current = [
            r
            for r in sorted(snap.raised.values(), key=lambda r: r.event.seq)
            if r.finding.agent_id == audit_trail_reviewer.AGENT_ID and snap.current(r.finding_id)
        ]
        by_check = Counter(r.check.partition(":")[0] for r in current)
        chain = [r for r in current if r.check.startswith(audit.CHAIN_LINKAGE)]
        lw = last.payload if last is not None else None
        assert lw is None or isinstance(lw, ActorWoke)
        return AuditView(
            agent_id=audit_trail_reviewer.AGENT_ID,
            agent_version=None if lw is None else lw.agent_version,
            last_review_seq=None if last is None else last.seq,
            last_review_at=None if last is None else last.recorded_at,
            review_from_seq=None if lw is None else lw.review_from_seq,
            review_to_seq=None if lw is None else lw.review_to_seq,
            review_completed=completed,
            chain_linkage="not_reviewed"
            if last is None or not completed
            else ("broken" if chain else "intact"),
            chain_findings=len(chain),
            by_check=[CheckCount(check=c, count=by_check.get(c, 0)) for c in audit.CHECKS],
            findings=[
                AuditFindingItem(
                    finding_id=r.finding_id,
                    check=r.check.partition(":")[0],
                    severity=r.finding.severity,
                    status=snap.status(r.finding_id),
                    title=r.finding.title,
                    actor_under_review=r.finding.actor_under_review,
                )
                for r in current
            ],
        )

    def unblinded_cases(self, counter: Callable[[str], tuple[int, int]]) -> UnblindedCases:
        """The unblinded statistician only: how many cases of the operational cut carry the
        treatment field. counter(cut) gives (cases, cases with treatment)."""
        if not visibility.can_see_unblinded_counts(self.role):
            raise ForbiddenError(f"{self.role.value} may not see unblinded counts")
        cut_id = next(
            (
                c.cut_id
                for c in self.snapshot().cuts.values()
                if self.scope(c.cut_id) == "operational"
            ),
            None,
        )
        if cut_id is None:
            raise NotFoundError("no operational cut")
        try:
            cases, with_treatment = counter(cut_id)
        except (MissingTableError, StudyAccessError, OSError) as exc:
            raise NotFoundError("safety database not available for the cut") from exc
        return UnblindedCases(cut_id=cut_id, cases=cases, cases_with_treatment=with_treatment)

    def portfolio(self) -> Portfolio:
        open_count = sum(1 for item in self.review_queue().items if item.study_id == self._study_id)
        return Portfolio(
            assets=[
                PortfolioAsset(
                    asset_id=a.asset_id,
                    static=a.static,
                    live=AssetLive(open_findings=open_count)
                    if a.asset_id == self._study_id
                    else None,
                )
                for a in sorted(self._portfolio, key=lambda a: a.asset_id)
            ]
        )

    # ---- deciding

    def decide(
        self, finding_id: str, action: DecisionAction, reason: str | None
    ) -> DecisionResponse:
        r = self._visible(finding_id)  # 404 before anything else: existence is not leaked
        if self.role is Role.QA_AUDITOR:
            raise ForbiddenError("qa_auditor may review findings but may never make a decision")
        if not visibility.can_decide(self.role, r.finding):
            deciders = ", ".join(sorted(visibility.DECIDERS.get(r.finding.kind, ())))
            raise ForbiddenError(
                f"{self.role.value} may not decide {r.finding.kind.value} findings; "
                f"{deciders} decide them"
            )
        conflict = visibility.self_decision(self.person.actor, r.finding)
        if conflict is not None:
            raise ForbiddenError(conflict)
        if action is DecisionAction.DISMISSED and (reason is None or not reason.strip()):
            raise InvalidDecisionError("a dismissal needs a non-empty reason")
        decision = Decision(
            finding_id=finding_id,
            action=action,
            actor=self.person.actor,
            role=self.role,
            reason=reason.strip() if reason else None,
        )
        draft = EventDraft(
            occurred_at=self._clock.now(),
            actor=self.person.actor,
            study_id=r.finding.study_id,
            visibility=r.finding.visibility,  # a decision is as visible as its finding
            payload=HumanDecision(decision=decision),
        )

        def still_open() -> None:
            history = [
                e
                for e in self._ledger.read(
                    event_types=[EventType.HUMAN_DECISION],
                    viewer_role=visibility.ledger_viewer(self.role),
                )
                if isinstance(e, Event)
                and isinstance(e.payload, HumanDecision)
                and e.payload.decision.finding_id == finding_id
            ]
            if history:
                last = history[-1].payload
                assert isinstance(last, HumanDecision)
                if last.decision.action in CLOSED:
                    raise ConflictError(
                        f"finding {finding_id} is already {last.decision.action.value}"
                    )

        event = self._ledger.append_guarded(draft, still_open)
        self._snapshot = None
        return DecisionResponse(
            finding_id=finding_id, status=action.value, decision=_decision_view(event)
        )


def _agent_of(e: LedgerEntry) -> str | None:
    """The agent an event is about, for the ledger's agent filter."""
    if not isinstance(e, Event):
        return None
    p = e.payload
    if isinstance(p, ActorWoke | ActorSuspended | FindingNarrative | FindingAssessment):
        return p.agent_id
    if isinstance(p, FindingRaised):
        return p.finding.agent_id
    return None


def _model_payload(e: Event) -> FindingNarrative | FindingAssessment:
    p = e.payload
    assert isinstance(p, FindingNarrative | FindingAssessment)
    return p


def _first_seq(e: Event) -> int:
    p = _model_payload(e)
    return p.finding_seq if isinstance(p, FindingNarrative) else p.finding_seqs[0]


def _woke(run: _Run) -> ActorWoke:
    p = run.woke.payload
    assert isinstance(p, ActorWoke)
    return p


def _trigger(e: Event) -> int | None:
    p = e.payload
    return p.trigger_seq if isinstance(p, ActorWoke | ActorSuspended) else None


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""
