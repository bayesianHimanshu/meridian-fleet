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
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from meridian.agents.rbqm import narrator
from meridian.agents.registry import AgentRegistry
from meridian.api import visibility
from meridian.api.identity import Person
from meridian.api.models import (
    AssetLive,
    AssetStatic,
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
    Portfolio,
    PortfolioAsset,
    RbqmSites,
    ReviewQueue,
    RunEvent,
    RunTrace,
    StudySummary,
    VerifyReport,
)
from meridian.api.sites import site_metrics
from meridian.api.study import enrollment
from meridian.data.study_store import MissingTableError, StudyAccessError, StudyReader
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import DecisionAction, EventType, Role
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    ContainmentCheckCompleted,
    DataCut,
    Event,
    EventDraft,
    FindingNarrative,
    FindingRaised,
    HumanDecision,
    LedgerEntry,
)
from meridian.domain.findings import Decision, Finding
from meridian.ledger.store import LedgerReader, LedgerWriter, RawLedgerSource
from meridian.ledger.verify import verify
from meridian.platform.clock import Clock
from meridian.platform.config import AgentsSettings
from meridian.sandbox.policies import ALL_POLICIES, NARRATOR_POLICY, policy_path

OPAQUE_CUT = re.compile(r"^DC-[0-9a-f]{8}$")
CLOSED: frozenset[DecisionAction] = frozenset({DecisionAction.ACCEPTED, DecisionAction.DISMISSED})
LEDGER_PAGE_MAX = 1000


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
    decisions: dict[str, list[Event]] = field(default_factory=lambda: defaultdict(list))
    woke: list[Event] = field(default_factory=list)
    suspended: list[Event] = field(default_factory=list)
    containment: list[Event] = field(default_factory=list)
    # finding id -> id of the newest-version finding with the same (agent, cut, kind, site,
    # check), for every finding a newer version superseded.
    superseded_by: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, entries: Sequence[LedgerEntry], role: Role) -> Snapshot:
        snap = cls(role=role)
        narratives: list[Event] = []
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
        for e in decisions:
            assert isinstance(e.payload, HumanDecision)
            if e.payload.decision.finding_id in snap.raised:
                snap.decisions[e.payload.decision.finding_id].append(e)
        snap.superseded_by = _supersession(snap.raised.values())
        return snap

    def status(self, finding_id: str) -> FindingStatus:
        history = self.decisions.get(finding_id, [])
        if not history:
            return "open"
        last = history[-1].payload
        assert isinstance(last, HumanDecision)
        status: FindingStatus = last.decision.action.value
        return status


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
            policy_sha256=p.policy_sha256,
            inputs_manifest_sha256=p.inputs_manifest_sha256,
        )
    assert isinstance(p, ActorWoke)
    return RunEvent(seq=e.seq, hash=e.hash, recorded_at=e.recorded_at, executor=p.executor)


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
    ) -> None:
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
            )
            for e in snap.narratives.get(finding_id, [])
            if isinstance((p := e.payload), FindingNarrative)
        ]
        return FindingDetail(
            finding=self._summary(r),
            dedup_key=r.dedup_key,
            hash=r.event.hash,
            evidence=list(r.finding.evidence),
            narratives=narratives,
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
    ) -> LedgerPage:
        """asc: events after since, oldest first. desc: the newest events before `before`
        (or the newest of all), newest first, for a reader paging back in time."""
        entries = self._ledger.read(
            since_seq=since, event_types=types, viewer_role=visibility.ledger_viewer(self.role)
        )
        if order == "desc":
            older = [e for e in entries if before is None or e.seq < before]
            page_desc = [visibility.redact(self.role, e) for e in reversed(older[-limit:])]
            more_older = len(older) > limit
            return LedgerPage(
                entries=page_desc,
                next_since=None,
                next_before=page_desc[-1].seq if more_older and page_desc else None,
            )
        shown = [visibility.redact(self.role, e) for e in entries[: limit + 1]]
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
        agents: list[FleetAgent] = []
        for registered in self._registry.all():
            a = registered.agent
            policy = policy_path(a.zone.value, self._policies_dir)
            woke = [
                e
                for e in snap.woke
                if isinstance(e.payload, ActorWoke)
                and (e.payload.agent_id, e.payload.agent_version) == (a.agent_id, a.version)
            ]
            last = woke[-1] if woke else None
            outcome: Literal["completed", "not_completed"] | None = None
            if last is not None:
                assert isinstance(last.payload, ActorWoke)
                lw = last.payload
                done = any(
                    isinstance(e.payload, ActorSuspended)
                    and e.seq > last.seq
                    and (e.payload.agent_id, e.payload.agent_version, e.payload.trigger_seq)
                    == (lw.agent_id, lw.agent_version, lw.trigger_seq)
                    for e in snap.suspended
                )
                outcome = "completed" if done else "not_completed"
            agents.append(
                FleetAgent(
                    agent_id=a.agent_id,
                    version=a.version,
                    zone=a.zone.value,
                    role="detector",
                    enabled=registered.enabled,
                    policy_file=policy.as_posix(),
                    policy_sha256=_file_sha256(policy),
                    last_run_at=None if last is None else last.recorded_at,
                    last_outcome=outcome,
                    last_trigger_seq=None if last is None else _trigger(last),
                )
            )
        narratives = sorted(
            (
                e
                for es in snap.narratives.values()
                for e in es
                if isinstance(e.payload, FindingNarrative)
                and e.payload.agent_version == narrator.VERSION
            ),
            key=lambda e: e.seq,
        )
        last_n = narratives[-1] if narratives else None
        narrator_policy = policy_path(NARRATOR_POLICY, self._policies_dir)
        agents.append(
            FleetAgent(
                agent_id=narrator.AGENT_ID,
                version=narrator.VERSION,
                zone=ALL_POLICIES[NARRATOR_POLICY].zone,
                role="narrator",
                enabled=True,
                policy_file=narrator_policy.as_posix(),
                policy_sha256=_file_sha256(narrator_policy),
                last_run_at=None if last_n is None else last_n.recorded_at,
                last_outcome=None
                if last_n is None or not isinstance(last_n.payload, FindingNarrative)
                else last_n.payload.status,
                last_trigger_seq=None
                if last_n is None or not isinstance(last_n.payload, FindingNarrative)
                else last_n.payload.finding_seq,
            )
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
                    policy_sha256=p.policy_sha256,
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

    def flagged_checks(self, cut_id: str) -> dict[str, list[str]]:
        """Checks flagged per site on this cut by the registered detector versions, as the
        caller may see them."""
        current = {r.agent.agent_id: r.agent.version for r in self._registry.all()}
        out: dict[str, set[str]] = defaultdict(set)
        for r in self.snapshot().raised.values():
            f = r.finding
            if r.cut_id == cut_id and current.get(f.agent_id) == f.agent_version:
                out[f.site_id or "STUDY"].add(r.check)
        return {site: sorted(checks) for site, checks in out.items()}

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


def _trigger(e: Event) -> int | None:
    p = e.payload
    return p.trigger_seq if isinstance(p, ActorWoke | ActorSuspended) else None


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""
