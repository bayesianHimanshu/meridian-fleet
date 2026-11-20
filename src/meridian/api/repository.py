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
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

import polars as pl
from pydantic import ValidationError

from meridian.agents.base import zone_of
from meridian.agents.quality import audit_trail_reviewer, tmf_completeness
from meridian.agents.rbqm import narrator
from meridian.agents.rbqm.describe import describe
from meridian.agents.registry import AgentRegistry
from meridian.agents.safety import local_narrator, term_adjudicator
from meridian.api import preclinical, visibility
from meridian.api import safety as safety_view
from meridian.api import tmf as tmf_view
from meridian.api.identity import Person
from meridian.api.models import (
    NOAEL_RATIONALE_MIN,
    AnswerView,
    AssessmentView,
    AssetLive,
    AssetStatic,
    AuditFindingItem,
    AuditView,
    ChainLink,
    CheckCount,
    ContainmentCheckView,
    ContainmentView,
    CorrectionView,
    CutKind,
    CutScope,
    CutSummary,
    DecisionResponse,
    DecisionView,
    DoseStep,
    DoseTranslationView,
    Enrollment,
    EvidenceChangedSince,
    FindingDetail,
    FindingPage,
    FindingStatus,
    FindingSummary,
    Fleet,
    FleetAgent,
    GateCriterionDef,
    GateDecisionBody,
    GateDecisionView,
    GateDef,
    GateRequestBody,
    GateRequestView,
    GatesView,
    IndReadinessView,
    LabelReaction,
    LatestNarrative,
    LedgerPage,
    MarginView,
    MonitoringBrief,
    MonitoringItemView,
    MonitoringSourceView,
    NarrativeView,
    NoaelBrief,
    NoaelCandidate,
    NoaelDecisionView,
    NoaelHistory,
    NoaelRequest,
    ObservationItem,
    ObservationRequest,
    ObservationResponse,
    OutlookView,
    Portfolio,
    PortfolioAsset,
    PreclinicalEndpoints,
    PreclinicalStudy,
    PreviousDecision,
    ProposalView,
    RbqmSites,
    RecommendationView,
    Reconciliation,
    Recorded,
    RequirementView,
    ReviewQueue,
    ReviewRequest,
    RunEvent,
    RunTrace,
    SafetySignalsView,
    ScanSummary,
    SignalRow,
    SiteFlag,
    StartingDose,
    StudySummary,
    TmfCompletenessView,
    TmfFindingItem,
    TranslationalView,
    UnblindedCases,
    VerifyReport,
)
from meridian.api.sites import site_metrics
from meridian.api.study import enrollment
from meridian.data.study_store import MissingTableError, StudyAccessError, StudyReader
from meridian.domain import audit
from meridian.domain.audit import LEDGER_SCOPE
from meridian.domain.dedup import make_dedup_key, parse_dedup_key
from meridian.domain.display import sig
from meridian.domain.enums import DecisionAction, EventType, FindingKind, Role, Visibility
from meridian.domain.events import (
    GATE_PREFIX,
    INVENTORY_CUT_PREFIX,
    REVIEW_PREFIX,
    SCAN_CUT_PREFIX,
    ActorSuspended,
    ActorWoke,
    AttributionCorrection,
    ContainmentCheckCompleted,
    DataCut,
    Event,
    EventDraft,
    FindingAssessment,
    FindingNarrative,
    FindingRaised,
    GateDecision,
    GatePack,
    GateRequest,
    HumanDecision,
    InventoryLoaded,
    LedgerEntry,
    NoaelDecision,
    PortfolioOutlook,
    PortfolioReviewRequested,
    QualityObservation,
    SendStudyLoaded,
    SignalScanRequested,
)
from meridian.domain.findings import Decision, Evidence, Finding
from meridian.ledger.store import LedgerReader, LedgerWriter, RawLedgerSource
from meridian.ledger.verify import verify
from meridian.platform.agent_settings import (
    AuditTrailReviewerConfig,
    IndReadinessConfig,
    SignalScoutConfig,
    TmfCompletenessConfig,
    ToxStudyMonitorConfig,
)
from meridian.platform.clock import Clock
from meridian.platform.config import AgentsSettings
from meridian.portfolio.gates import Gates, pack_for_display
from meridian.portfolio.outlook import for_display
from meridian.preclinical import readiness
from meridian.preclinical.readiness import INVENTORY_TABLE
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


def correction_view(e: Event) -> CorrectionView:
    p = e.payload
    assert isinstance(p, AttributionCorrection)
    return CorrectionView(
        seq=e.seq,
        corrects_seq=p.corrects_seq,
        field=p.field,
        recorded_value=p.recorded_value,
        true_value=p.true_value,
        explanation=p.explanation,
        corrected_by=p.corrected_by,
        recorded_at=e.recorded_at,
    )


@dataclass(slots=True)
class Snapshot:
    """The ledger as one role sees it, indexed."""

    role: Role
    cuts: dict[int, DataCut] = field(default_factory=dict)
    # SEND study loads (phase 5): trigger seq -> SEND cut id, so a newer version's run on a
    # SEND cut retires what it no longer raises, as a run on a DataCut does.
    send_loads: dict[int, str] = field(default_factory=dict)
    # Inventory loads (phase 5 step 2): trigger seq -> (asset, inventory cut id).
    inventory_loads: dict[int, tuple[str, str]] = field(default_factory=dict)
    # Signal scans (phase 5 step 3a): trigger seq -> scan id.
    scans: dict[int, str] = field(default_factory=dict)
    scan_events: dict[int, SignalScanRequested] = field(default_factory=dict)
    # Phase 5 step 3b: AttributionCorrections by the seq they correct.
    corrections: dict[int, list[Event]] = field(default_factory=lambda: defaultdict(list))
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
    # finding id -> the closing decision on an earlier version's finding with the same
    # (agent, cut, kind, site, check), for a finding with no decision of its own. Split by
    # whether the primary evidence is unchanged since that decision.
    previously_decided: dict[str, PriorDecision] = field(default_factory=dict)
    changed_since: dict[str, PriorDecision] = field(default_factory=dict)

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
            elif isinstance(p, SendStudyLoaded):
                snap.send_loads[shown.seq] = p.cut_id
            elif isinstance(p, InventoryLoaded):
                snap.inventory_loads[shown.seq] = (p.asset_id, p.cut_id)
            elif isinstance(p, SignalScanRequested):
                snap.scans[shown.seq] = p.scan_id
                snap.scan_events[shown.seq] = p
            elif isinstance(p, AttributionCorrection):
                snap.corrections[p.corrects_seq].append(shown)
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
        _latest_wins(snap)
        snap.previously_decided, snap.changed_since = _prior_decisions(snap)
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

    def observation_ids(self) -> set[str]:
        """The observations whose qa_observation finding this role may see."""
        ids = (visibility.observation_id_of(r.finding) for r in self.raised.values())
        return {i for i in ids if i is not None}

    def closing_decision(self, finding_id: str) -> Event | None:
        """The finding's latest decision when it accepted or dismissed it, else None."""
        history = self.decisions.get(finding_id, [])
        if not history:
            return None
        last = history[-1].payload
        assert isinstance(last, HumanDecision)
        return history[-1] if last.decision.action in CLOSED else None

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


def _prior_view(p: PriorDecision | None) -> PreviousDecision | None:
    if p is None:
        return None
    e = p.decision
    assert isinstance(e.payload, HumanDecision)
    d = e.payload.decision
    return PreviousDecision(
        finding_id=p.finding_id,
        agent_version=p.agent_version,
        action=d.action,
        actor=d.actor,
        role=d.role,
        reason=d.reason,
        recorded_at=e.recorded_at,
        seq=e.seq,
        primary_evidence_sha256=p.evidence_sha256,
    )


def _changed_view(p: PriorDecision | None, f: Finding) -> EvidenceChangedSince | None:
    view = _prior_view(p)
    if p is None or view is None:
        return None
    now = primary_displays(f)
    return EvidenceChangedSince(
        decision=view,
        before=p.evidence,
        after=now,
        removed=[x for x in p.evidence if x not in now],
        added=[x for x in now if x not in p.evidence],
        primary_evidence_sha256=primary_evidence_sha256(f),
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
            substrate=p.substrate,
        )
    assert isinstance(p, ActorWoke)
    return RunEvent(
        seq=e.seq,
        hash=e.hash,
        recorded_at=e.recorded_at,
        executor=p.executor,
        substrate=p.substrate,
    )


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
    cut_of = {seq: cut.cut_id for seq, cut in snap.cuts.items()} | snap.send_loads
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


def _latest_wins(snap: Snapshot) -> None:
    """Phase 5 step 2, for the agents whose newest input replaces the old one rather than
    adding a cut beside it.

    * A dose proposal rests on the study's current NOAEL: the newest proposal of a study
      (by seq) is current and supersedes every earlier one (a revised NOAEL wakes the agent
      again; docs/fih-dose-notes.md).
    * An IND readiness gap belongs to an inventory: the newest inventory cut the agent
      completed a run on is current. A gap on an older cut is superseded by the same check
      on that cut, or retired (by the version of that run) when the newer cut lacks it.
    """
    proposals: dict[tuple[str, str], list[_Raised]] = defaultdict(list)
    readiness: dict[tuple[str, str], list[_Raised]] = defaultdict(list)
    signals: dict[tuple[str, str], list[_Raised]] = defaultdict(list)
    for r in snap.raised.values():
        f = r.finding
        if f.kind in (FindingKind.DOSE_PROPOSAL, FindingKind.MONITORING_RECOMMENDATION):
            proposals[(f.agent_id, f.study_id)].append(r)
        elif f.kind is FindingKind.IND_READINESS:
            readiness[(f.agent_id, f.study_id)].append(r)
        elif f.kind is FindingKind.REPORTING_SIGNAL:
            signals[(f.agent_id, f.study_id)].append(r)
    for members in proposals.values():
        newest = max(members, key=lambda r: r.event.seq)
        for r in members:
            if r is not newest:
                snap.superseded_by[r.finding_id] = newest.finding_id
                snap.retired_by.pop(r.finding_id, None)
    runs: dict[tuple[str, str], tuple[int, str]] = {}  # (agent, asset) -> (seq, cut, version)
    versions: dict[tuple[str, int], str] = {}
    for e in snap.suspended:
        p = e.payload
        if isinstance(p, ActorSuspended) and p.trigger_seq in snap.inventory_loads:
            asset, cut = snap.inventory_loads[p.trigger_seq]
            key = (p.agent_id, asset)
            if key not in runs or p.trigger_seq > runs[key][0]:
                runs[key] = (p.trigger_seq, cut)
            versions[(p.agent_id, p.trigger_seq)] = p.agent_version or ""
    for (agent, asset), members in readiness.items():
        latest = runs.get((agent, asset))
        if latest is None:
            continue
        seq, cut = latest
        on_latest = {r.check: r for r in members if r.cut_id == cut}
        for r in members:
            if r.cut_id == cut:
                continue
            twin = on_latest.get(r.check)
            if twin is not None:
                snap.superseded_by[r.finding_id] = twin.finding_id
                snap.retired_by.pop(r.finding_id, None)
            elif r.finding_id not in snap.superseded_by:
                snap.retired_by[r.finding_id] = versions.get((agent, seq), "")
    _latest_scan_wins(snap, signals)


def _latest_scan_wins(snap: Snapshot, signals: dict[tuple[str, str], list[_Raised]]) -> None:
    """Phase 5 step 3a: the newest signal scan the agent completed is current; a signal of
    an older scan is superseded by the same term's finding in it, or retired."""
    latest: dict[str, tuple[int, str]] = {}  # agent -> (trigger seq, version)
    for e in snap.suspended:
        p = e.payload
        if (
            isinstance(p, ActorSuspended)
            and p.trigger_seq in snap.scans
            and (p.agent_id not in latest or p.trigger_seq > latest[p.agent_id][0])
        ):
            latest[p.agent_id] = (p.trigger_seq, p.agent_version or "")
    for (agent, _study), members in signals.items():
        if agent not in latest:
            continue
        seq, version = latest[agent]
        cut = snap.scans[seq]
        on_latest = {r.check: r for r in members if r.cut_id == cut}
        for r in members:
            if r.cut_id == cut:
                continue
            twin = on_latest.get(r.check)
            if twin is not None:
                snap.superseded_by[r.finding_id] = twin.finding_id
            else:
                snap.retired_by[r.finding_id] = version


def primary_displays(f: Finding) -> list[str]:
    """Display strings of the primary evidence, described the same way for old findings."""
    return [e.display for e in describe(f.evidence) if e.role == "primary" and e.display]


def primary_evidence_sha256(f: Finding) -> str:
    """Canonical hash of the primary evidence: its display values, sorted (an order change
    between agent versions is not a change of evidence), as compact JSON."""
    canonical = json.dumps(sorted(primary_displays(f)), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PriorDecision:
    """A person's closing decision on an earlier version's finding with the same identity."""

    finding_id: str
    agent_version: str
    decision: Event  # the HumanDecision that accepted or dismissed it
    evidence: list[str]  # that finding's primary evidence display values
    evidence_sha256: str


def _prior_decisions(
    snap: Snapshot,
) -> tuple[dict[str, PriorDecision], dict[str, PriorDecision]]:
    """For each finding nobody has decided: the latest closing decision (accepted or
    dismissed, by ledger seq) on a finding an earlier version of the same agent raised with
    the same (cut, kind, site, check). Unchanged primary evidence (equal canonical hashes,
    and not empty) puts the finding in the first map, which keeps it out of the queue;
    otherwise in the second, so its page can show what changed. An earlier finding that is
    open or escalated never counts. Derived at read time over what the caller may see; no
    event is rewritten."""
    groups: dict[tuple[str, str, str, str, str], list[_Raised]] = defaultdict(list)
    for r in snap.raised.values():
        f = r.finding
        groups[(f.agent_id, r.cut_id, f.kind.value, f.site_id or "STUDY", r.check)].append(r)
    unchanged: dict[str, PriorDecision] = {}
    changed: dict[str, PriorDecision] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        closed = [(r, d) for r in members if (d := snap.closing_decision(r.finding_id))]
        if not closed:
            continue
        for r in members:
            if snap.decisions.get(r.finding_id):
                continue  # its own decisions govern it
            own = _version(r.finding.agent_version)
            earlier = [(e, d) for e, d in closed if _version(e.finding.agent_version) < own]
            if not earlier:
                continue
            e, d = max(earlier, key=lambda pair: pair[1].seq)
            prior = PriorDecision(
                finding_id=e.finding_id,
                agent_version=e.finding.agent_version,
                decision=d,
                evidence=primary_displays(e.finding),
                evidence_sha256=primary_evidence_sha256(e.finding),
            )
            same = prior.evidence and prior.evidence_sha256 == primary_evidence_sha256(r.finding)
            (unchanged if same else changed)[r.finding_id] = prior
    return unchanged, changed


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
    """Defaults: operational cuts only; superseded, retired and previously decided findings
    left out."""

    scope: ScopeFilter = "operational"
    include_superseded: bool = False
    include_retired: bool = False
    include_previously_decided: bool = False
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
        executors: Mapping[str, str] | None = None,
        default_executor: str = "openshell",
        send_reader: Callable[[str, str], StudyReader] | None = None,
        tox_config: ToxStudyMonitorConfig | None = None,
        preclinical_assets: Mapping[str, str] | None = None,
        inventory_reader: Callable[[str, str], StudyReader] | None = None,
        ind_config: IndReadinessConfig | None = None,
        signal_responses: Callable[[int], pl.DataFrame | None] | None = None,
        signal_config: SignalScoutConfig | None = None,
        gates: Gates | None = None,
    ) -> None:
        # Phase 5 step 3b: the stage gates (config/gates.toml), for gate requests.
        self._gates = gates
        # Phase 5 step 3a: a scan's kept openFDA responses, and the scout's config.
        self._signal_responses = signal_responses
        self._signal_config = signal_config
        # Phase 5 step 2: (asset, inventory cut) to its reader, and the readiness rules.
        self._inventory_reader = inventory_reader
        self._ind_config = ind_config
        # Phase 5: (study, cut) to a SEND study reader, the agent's config (the endpoint
        # tables use its analysis), and preclinical asset id to its SEND study id.
        self._send_reader = send_reader
        self._tox_config = tox_config
        self._preclinical_assets = dict(preclinical_assets or {})
        self._executors = dict(executors or {})
        self._default_executor = default_executor
        # A started detector run without its ActorSuspended counts as running, not failed,
        # until this many seconds after it started ([runtime] exec_timeout_seconds).
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
        if cut_id == LEDGER_SCOPE or cut_id.startswith(
            (INVENTORY_CUT_PREFIX, SCAN_CUT_PREFIX, REVIEW_PREFIX, GATE_PREFIX)
        ):
            return "operational"
        return "operational" if cut_id in self._operational else "evaluation"

    # ---- reading

    @property
    def study_id(self) -> str:
        return self._study_id

    def preclinical_assets(self) -> Mapping[str, str]:
        """Preclinical asset id to its SEND study id."""
        return dict(self._preclinical_assets)

    def noael_views(self, study_id: str) -> list[NoaelDecisionView]:
        """A study's NOAEL decisions as this role sees them (preclinical roles only)."""
        self._require_preclinical()
        return self._noael_views(study_id)

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
            previously_decided=_prior_view(snap.previously_decided.get(r.finding_id)),
            evidence_changed_since=_changed_view(snap.changed_since.get(r.finding_id), f),
            primary_evidence=primary_displays(f),
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
            raised_by=f.raised_by,
            actor_under_review=f.actor_under_review,
            may_decide=self._blocked(f) is None,
            decide_blocked_reason=self._blocked(f),
        )

    def _blocked(self, f: Finding) -> str | None:
        """Why the caller may not decide f, or None (the rules decide() enforces)."""
        if not visibility.can_decide(self.role, f):
            if self.role is Role.QA_AUDITOR:
                return "qa_auditor may review findings but may never make a decision"
            deciders = ", ".join(sorted(visibility.DECIDERS.get(f.kind, ())))
            return (
                f"{self.role.value} may not decide {f.kind.value} findings; {deciders} decide them"
            )
        return visibility.self_decision(self.person.actor, f)

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
            if not filters.include_previously_decided and r.finding_id in snap.previously_decided:
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
        agent, version and DataCut: the runner writes woke, findings, suspended in order.
        A finding about the ledger itself (cut LEDGER, an audit review) has no DataCut: its
        run is the agent's last ActorWoke before the finding, whatever its trigger."""
        snap = self.snapshot()
        f = r.finding
        trigger = next((seq for seq, c in snap.cuts.items() if c.cut_id == r.cut_id), None)
        if trigger is None:
            prior = [
                e.payload.trigger_seq
                for e in snap.woke
                if isinstance(e.payload, ActorWoke)
                and (e.payload.agent_id, e.payload.agent_version) == (f.agent_id, f.agent_version)
                and e.seq < r.event.seq
            ]
            if not prior:
                return None
            trigger = prior[-1]

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
        no superseded, retired or previously decided findings, so evaluation cuts never
        reach it unless asked for."""
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
        snap = self.snapshot()
        visible = snap.raised.keys()
        observations = snap.observation_ids()

        def shown_as(e: LedgerEntry) -> LedgerEntry:
            return visibility.redact_with_findings(self.role, e, visible, observations)

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
                corrections=self._corrections_of(page_desc, snap),
            )
        shown = [shown_as(e) for e in entries[: limit + 1]]
        more = len(shown) > limit
        page = shown[:limit]
        return LedgerPage(
            entries=page,
            next_since=page[-1].seq if more and page else None,
            corrections=self._corrections_of(page, snap),
        )

    @staticmethod
    def _corrections_of(
        page: Sequence[LedgerEntry], snap: Snapshot
    ) -> dict[int, list[CorrectionView]]:
        """The corrections of the events shown in full on a page (phase 5 step 3b)."""
        return {
            e.seq: [correction_view(c) for c in snap.corrections[e.seq]]
            for e in page
            if isinstance(e, Event) and snap.corrections.get(e.seq)
        }

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
            return r.suspended is None and now - _started(r) <= self._run_timeout

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
            recent = [r for r in mine if _started(r) >= since]
            failures = [r for r in mine if failed(r)]
            done = [r for r in current if r.suspended is not None]
            last_done = done[-1].suspended.payload if done and done[-1].suspended else None
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
                    last_run_at=None if last is None else _started(last),
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
                    last_failure_at=_started(failures[-1]) if failures else None,
                    run_event_types=[EventType.ACTOR_WOKE, EventType.ACTOR_SUSPENDED],
                    executor=self._executors.get(policy_name, self._default_executor),
                    substrate=last_done.substrate
                    if isinstance(last_done, ActorSuspended)
                    else None,
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
        latest: dict[tuple[str, str], Event] = {}
        for e in snap.containment:
            assert isinstance(e.payload, ContainmentCheckCompleted)
            key = (e.payload.policy_name or e.payload.zone, e.payload.executor or "openshell")
            latest[key] = e
        containment = []
        for policy_name, executor in sorted(latest):
            e = latest[(policy_name, executor)]
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
                    executor=executor,
                    substrate=p.substrate,
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
        study_id = self._study_id
        refers: list[Evidence] = []
        # An observation on preclinical work (a NOAEL decision or a preclinical finding)
        # reaches the preclinical roles, so the toxicologist sees it; no clinical role does.
        referred_entry = (
            self._visible_entry(body.ledger_seq, snap) if body.ledger_seq is not None else None
        )
        referred_finding = self._visible(body.finding_id) if body.finding_id is not None else None
        if (referred_entry is not None and isinstance(referred_entry.payload, NoaelDecision)) or (
            referred_finding is not None
            and referred_finding.finding.agent_id.startswith("preclinical.")
        ):
            visible_to = set(visibility.PRECLINICAL_ROLES)
        if body.finding_id is not None:
            referred = self._visible(body.finding_id)
            study_id = referred.finding.study_id
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
            if isinstance(p, NoaelDecision):
                actor_under_review = p.decided_by  # the toxicologist does not decide it
                study_id = p.study_id
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
            study_id=study_id,
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
            study_id=study_id,
            site_id=None,
            cut_id=LEDGER_SCOPE,
            check=f"qa_observation:{observation_id}",
        )
        drafts = [
            EventDraft(
                occurred_at=now,
                actor=self.person.actor,
                study_id=study_id,
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
        shown = visibility.redact_with_findings(
            self.role, entry, snap.raised, snap.observation_ids()
        )
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
        queue = self.review_queue().items
        open_count = sum(1 for item in queue if item.study_id == self._study_id)

        def live(asset_id: str) -> AssetLive | None:
            if asset_id == self._study_id:
                return AssetLive(open_findings=open_count)
            study = self._preclinical_assets.get(asset_id)
            if study is None:
                return None
            if not visibility.can_see_preclinical(self.role):
                return AssetLive(
                    open_findings=0,
                    noael_status="not_shown",
                    ind_status="not_shown",
                    starting_dose=self.starting_dose(study),
                    monitoring=self.monitoring_brief(study),
                )
            current = [v for v in self._noael_views(study) if v.current]
            briefs = [
                NoaelBrief(
                    decision_id=v.decision_id,
                    sex=v.sex,
                    established=v.established,
                    noael_dose=v.noael_dose,
                    dose_unit=v.dose_unit,
                    recorded_at=v.recorded_at,
                    decided_by=v.decided_by,
                )
                for v in sorted(current, key=lambda v: v.sex)
            ]
            ind = self.ind_readiness(asset_id) if self._ind_config is not None else None
            return AssetLive(
                open_findings=sum(1 for item in queue if item.study_id in (study, asset_id)),
                noael_status="decided" if briefs else "not_yet_decided",
                noael=briefs,
                ind_status=None if ind is None else ind.status,
                ind_readiness_percent=None if ind is None else ind.percent_met,
                starting_dose=self.starting_dose(study),
                monitoring=self.monitoring_brief(study),
            )

        return Portfolio(
            assets=[
                PortfolioAsset(asset_id=a.asset_id, static=a.static, live=live(a.asset_id))
                for a in sorted(self._portfolio, key=lambda a: a.asset_id)
            ]
        )

    # ---- preclinical (phase 5 step 1)

    def _require_preclinical(self) -> None:
        if not visibility.can_see_preclinical(self.role):
            raise ForbiddenError(f"{self.role.value} does not see preclinical studies")

    def _send_loads(self) -> dict[str, Event]:
        """The latest SendStudyLoaded event of each SEND study."""
        out: dict[str, Event] = {}
        for e in self._ledger.read(
            event_types=[EventType.SEND_STUDY_LOADED],
            viewer_role=visibility.ledger_viewer(self.role),
        ):
            if isinstance(e, Event) and isinstance(e.payload, SendStudyLoaded):
                out[e.payload.study_id] = e
        return out

    def _send_study(self, study_id: str) -> tuple[Event, StudyReader]:
        self._require_preclinical()
        loaded = self._send_loads().get(study_id)
        if loaded is None or self._send_reader is None:
            raise NotFoundError(f"preclinical study {study_id} not found")
        assert isinstance(loaded.payload, SendStudyLoaded)
        try:
            return loaded, self._send_reader(study_id, loaded.payload.cut_id)
        except (OSError, StudyAccessError) as exc:
            raise NotFoundError(f"preclinical study {study_id} is not on this host") from exc

    def preclinical_studies(self) -> list[PreclinicalStudy]:
        self._require_preclinical()
        asset_of = {s: a for a, s in self._preclinical_assets.items()}
        out = []
        for study_id in sorted(self._send_loads()):
            loaded, reader = self._send_study(study_id)
            assert isinstance(loaded.payload, SendStudyLoaded)
            out.append(
                preclinical.study_view(
                    reader,
                    study_id=study_id,
                    asset_id=asset_of.get(study_id),
                    cut_id=loaded.payload.cut_id,
                    seq=loaded.seq,
                )
            )
        return out

    def preclinical_endpoints(self, study_id: str) -> PreclinicalEndpoints:
        loaded, reader = self._send_study(study_id)
        assert isinstance(loaded.payload, SendStudyLoaded)
        if self._tox_config is None:
            raise NotFoundError("preclinical analysis is not configured")
        cut = loaded.payload.cut_id
        snap = self.snapshot()
        by_check = {
            r.check: fid
            for fid, r in snap.raised.items()
            if r.finding.study_id == study_id
            and r.cut_id == cut
            and r.finding.agent_id.startswith("preclinical.")
            and snap.current(fid)  # the newest version's, neither superseded nor retired
        }
        return preclinical.endpoints_view(
            reader, self._tox_config, study_id=study_id, cut_id=cut, finding_by_check=by_check
        )

    def _noael_views(self, study_id: str) -> list[NoaelDecisionView]:
        events = [
            e
            for e in self._ledger.read(
                event_types=[EventType.NOAEL_DECISION],
                viewer_role=visibility.ledger_viewer(self.role),
            )
            if isinstance(e, Event)
            and isinstance(e.payload, NoaelDecision)
            and e.payload.study_id == study_id
        ]
        return preclinical.noael_views(events)

    def _study_findings(self, study_id: str) -> list[_Raised]:
        """The current preclinical findings of a study this role may see, in id order."""
        snap = self.snapshot()
        return sorted(
            (
                r
                for fid, r in snap.raised.items()
                if r.finding.study_id == study_id
                and r.finding.agent_id.startswith("preclinical.")
                and r.finding.kind is FindingKind.TOX_SIGNAL
                and snap.current(fid)
            ),
            key=lambda r: r.finding_id,
        )

    def noael(self, study_id: str) -> NoaelHistory:
        _, reader = self._send_study(study_id)
        doses, unit = preclinical.main_doses(reader)
        views = self._noael_views(study_id)
        animals = preclinical.design_animals(reader)
        snap = self.snapshot()
        candidates = []
        for r in self._study_findings(study_id):
            prof = preclinical.finding_profile(r.finding, animals)
            candidates.append(
                NoaelCandidate(
                    finding_id=r.finding_id,
                    title=r.finding.title,
                    status=snap.status(r.finding_id),
                    sex=prof.sex,  # type: ignore[arg-type]
                    main_doses=list(prof.main_doses),
                    recovery_doses=list(prof.recovery_doses),
                )
            )
        return NoaelHistory(
            study_id=study_id,
            dose_unit=unit,
            doses=doses,
            current=[v for v in views if v.current],
            history=list(reversed(views)),
            may_record=visibility.can_record_noael(self.role),
            rationale_min_chars=NOAEL_RATIONALE_MIN,
            candidates=candidates,
        )

    def record_noael(self, study_id: str, body: NoaelRequest) -> NoaelDecisionView:
        """The toxicologist's NOAEL for a study and sex (or combined). Append-only: a
        revision names the current decision it supersedes (409 otherwise), and both stay."""
        self._require_preclinical()
        if not visibility.can_record_noael(self.role):
            raise ForbiddenError("only the toxicologist records a NOAEL decision")
        _, reader = self._send_study(study_id)
        doses, unit = preclinical.main_doses(reader)
        treated = [d for d in doses if d > 0]
        for name, dose in (("NOAEL", body.noael_dose), ("LOAEL", body.loael_dose)):
            if dose is not None and dose not in treated:
                raise InvalidDecisionError(
                    f"the {name} must be one of the study's dose levels: "
                    + ", ".join(f"{d:g}" for d in treated)
                    + f" {unit}"
                )
        # Phase 5 step 2: the rules the first NOAEL broke, checked here and not only in the
        # form. "Not established" names the LOAEL, the lowest dose (every dose was adverse).
        if not body.established and body.loael_dose is None:
            raise InvalidDecisionError(
                "a NOAEL that is not established needs the LOAEL: the lowest dose with an "
                "adverse finding"
            )
        if not body.established and body.loael_dose != min(treated):
            raise InvalidDecisionError(
                f"a NOAEL that is not established means an adverse finding at the lowest dose, "
                f"so the LOAEL is {min(treated):g} {unit}"
            )
        if (
            body.noael_dose is not None
            and body.loael_dose is not None
            and body.loael_dose <= body.noael_dose
        ):
            raise InvalidDecisionError("the LOAEL must be above the NOAEL")
        if len(body.rationale.strip()) < NOAEL_RATIONALE_MIN:
            raise InvalidDecisionError(
                f"a NOAEL rationale needs at least {NOAEL_RATIONALE_MIN} characters: what is "
                "adverse at the LOAEL and why the changes at the NOAEL are not (a finding's "
                "dismissal reason belongs on its own page)"
            )
        animals = preclinical.design_animals(reader)
        for fid in body.finding_ids:
            r = self._visible(fid)  # 404 if missing or not visible to the toxicologist
            if r.finding.study_id != study_id or not r.finding.agent_id.startswith("preclinical."):
                raise InvalidDecisionError(f"{fid} is not a finding of study {study_id}")
            if body.sex != "combined":
                sexes = preclinical.finding_profile(r.finding, animals).sexes
                if sexes != {body.sex}:
                    about = " and ".join(sorted(sexes)) if sexes else "no sex"
                    raise InvalidDecisionError(
                        f"{fid} is about {about}: a {body.sex} decision rests only on "
                        f"{body.sex} findings (any sex for a combined decision)"
                    )

        def current_id() -> str | None:
            mine = [v for v in self._noael_views(study_id) if v.sex == body.sex and v.current]
            return mine[-1].decision_id if mine else None

        def unchanged() -> None:
            if current_id() != body.supersedes:
                now = current_id()
                raise ConflictError(
                    f"the current {body.sex} NOAEL decision is {now}: a new decision must name "
                    "it in supersedes"
                    if now is not None
                    else f"there is no {body.sex} NOAEL decision to supersede"
                )

        unchanged()
        try:
            payload = NoaelDecision(
                decision_id="NOAEL-" + uuid.uuid4().hex[:12],
                study_id=study_id,
                sex=body.sex,
                established=body.established,
                noael_dose=body.noael_dose,
                loael_dose=body.loael_dose,
                dose_unit=unit,
                finding_ids=tuple(body.finding_ids),
                rationale=body.rationale.strip(),
                decided_by=self.person.actor,
                supersedes=body.supersedes,
            )
        except ValidationError as exc:
            raise InvalidDecisionError(
                "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
            ) from exc
        draft = EventDraft(
            occurred_at=self._clock.now(),
            actor=self.person.actor,
            study_id=study_id,
            visibility=Visibility.BLINDED,
            payload=payload,
        )
        event = self._ledger.append_guarded(draft, unchanged)
        self._snapshot = None
        (view,) = [v for v in self._noael_views(study_id) if v.seq == event.seq]
        return view

    # ---- the hand-off to the clinic (phase 5 step 2)

    def _proposals(self, study_id: str) -> tuple[_Raised | None, list[_Raised]]:
        """(the current dose proposal of a study, the earlier ones) as this role sees them."""
        snap = self.snapshot()
        mine = sorted(
            (
                r
                for r in snap.raised.values()
                if r.finding.kind is FindingKind.DOSE_PROPOSAL and r.finding.study_id == study_id
            ),
            key=lambda r: r.event.seq,
        )
        current = [r for r in mine if r.finding_id not in snap.superseded_by]
        return (current[-1] if current else None), [r for r in mine if r not in current]

    def _decided(self, finding_id: str) -> HumanDecision | None:
        history = self.snapshot().decisions.get(finding_id, [])
        if not history:
            return None
        last = history[-1].payload
        assert isinstance(last, HumanDecision)
        return last

    def _asset_of_study(self, study_id: str) -> str:
        return next((a for a, s in self._preclinical_assets.items() if s == study_id), study_id)

    def _proposal_title(self, r: _Raised) -> str:
        """The proposal's title, derived at read time from its values and its study's asset
        (phase 5 step 3a). dose_translation 1.0.0 recorded "Proposed MRD-110 starting dose";
        MRD-110 is a separate asset, and the dose is the asset's own first-in-human starting
        dose. The ledger keeps the recorded text (recorded_title)."""
        values = {e.statistic: e for e in r.finding.evidence}
        start = values.get("mrsd:starting_dose_mg")
        mrsd = values.get("mrsd:mrsd_mg_per_kg")
        asset = self._asset_of_study(r.finding.study_id)
        if start is None or mrsd is None:
            return r.finding.title
        weight = start.threshold if start.threshold is not None else start.value / mrsd.value
        return (
            f"Proposed first-in-human starting dose for {asset}: {sig(start.value)} mg for a "
            f"{weight:g} kg adult (MRSD {sig(mrsd.value)} mg/kg)"
        )

    def _proposal_view(self, r: _Raised) -> ProposalView:
        snap = self.snapshot()
        history = snap.decisions.get(r.finding_id, [])
        last = history[-1] if history else None
        decision = (
            last.payload.decision
            if last is not None and isinstance(last.payload, HumanDecision)
            else None
        )
        basis = next(
            (
                fields.removeprefix("DECISION=")
                for e in r.finding.evidence
                for fields in e.selector.split("; ")
                if fields.startswith("DECISION=") and e.role == "primary"
            ),
            None,
        )
        return ProposalView(
            finding_id=r.finding_id,
            seq=r.event.seq,
            title=self._proposal_title(r),
            recorded_title=r.finding.title,
            summary=r.finding.summary,
            check=r.check.split(":", 1)[0],
            status=snap.status(r.finding_id),
            basis_decision_id=basis,
            steps=[
                DoseStep(
                    statistic=e.statistic,
                    role=e.role,
                    value=e.value,
                    source=e.source,
                    selector=e.selector,
                    display=e.display or e.statistic,
                )
                for e in r.finding.evidence
            ],
            decided_by=None if decision is None else decision.actor,
            decided_seq=None if last is None else last.seq,
            decision_reason=None if decision is None else decision.reason,
        )

    def _recommendations(self, study_id: str) -> tuple[_Raised | None, list[_Raised]]:
        snap = self.snapshot()
        mine = sorted(
            (
                r
                for r in snap.raised.values()
                if r.finding.kind is FindingKind.MONITORING_RECOMMENDATION
                and r.finding.study_id == study_id
            ),
            key=lambda r: r.event.seq,
        )
        current = [r for r in mine if r.finding_id not in snap.superseded_by]
        return (current[-1] if current else None), [r for r in mine if r not in current]

    def _recommendation_view(self, r: _Raised) -> RecommendationView:
        snap = self.snapshot()
        history = snap.decisions.get(r.finding_id, [])
        last = history[-1] if history else None
        decision = (
            last.payload.decision
            if last is not None and isinstance(last.payload, HumanDecision)
            else None
        )
        ev = r.finding.evidence

        def fields(e: Evidence) -> dict[str, str]:
            return dict(p.split("=", 1) for p in e.selector.split("; ") if "=" in p)

        items: list[MonitoringItemView] = []
        for e in ev:
            if not e.statistic.startswith("monitoring:organ:"):
                continue
            organ = e.statistic.removeprefix("monitoring:organ:")
            text = {
                x.statistic.split(":")[1]: (x.display or "").split(": ", 1)[-1]
                for x in ev
                if x.statistic.endswith(f":{organ}")
                and x.statistic.split(":")[1] in ("measure", "frequency", "review", "stopping")
            }
            sources = []
            for x in ev:
                if x.statistic != f"monitoring:source:{organ}":
                    continue
                # The display is '<source>, <section>: "<quote>"'; a section may hold "; ",
                # the selector's separator, so it is read from the display.
                source = fields(x)["SOURCE"]
                head, _, quote = (x.display or "").partition(': "')
                sources.append(
                    MonitoringSourceView(
                        source=source,
                        section=head.removeprefix(f"{source}, "),
                        quote=quote.removesuffix('"'),
                    )
                )
            mine = [
                x for x in ev if x.statistic == "monitoring:finding" and fields(x)["ORGAN"] == organ
            ]
            items.append(
                MonitoringItemView(
                    organ=organ,
                    label=(e.display or organ).split(":", 1)[0],
                    measure=text.get("measure", ""),
                    frequency=text.get("frequency", ""),
                    review=text.get("review", ""),
                    stopping=text.get("stopping", ""),
                    sources=sources,
                    findings=[fields(x)["FINDING"] for x in mine],
                    finding_titles=[x.display or "" for x in mine],
                )
            )
        ratios = {
            e.statistic.split(":")[1].removesuffix("_margin"): e.value
            for e in ev
            if e.statistic.endswith("_margin")
        }
        margins = [
            MarginView(
                label="NOAEL" if e.statistic == "monitoring:noael_hed_mg" else "LOAEL",
                hed_mg=e.value,
                human_weight_kg=e.threshold,
                display=e.display or "",
                ratio=ratios.get(e.statistic.split(":")[1].removesuffix("_hed_mg")),
            )
            for e in ev
            if e.statistic in ("monitoring:noael_hed_mg", "monitoring:loael_hed_mg")
        ]
        start = next(
            (e.display or "" for e in ev if e.statistic == "monitoring:starting_dose_mg"), ""
        )
        basis = next((e.display or "" for e in ev if e.statistic == "monitoring:basis"), "")
        return RecommendationView(
            finding_id=r.finding_id,
            seq=r.event.seq,
            title=r.finding.title,
            status=snap.status(r.finding_id),
            decided_by=None if decision is None else decision.actor,
            decided_seq=None if last is None else last.seq,
            items=items,
            unmapped=[e.display or "" for e in ev if e.statistic == "monitoring:unmapped"],
            margins=margins,
            starting_dose=start,
            basis=basis,
        )

    def translational(self, study_id: str) -> TranslationalView:
        """The Translational panel: preclinical roles, as the rest of the study's page."""
        self._send_study(study_id)
        current, earlier = self._recommendations(study_id)
        superseded = [r.finding_id for r in earlier]
        if current is not None:
            return TranslationalView(
                study_id=study_id,
                status="recommended",
                why=(
                    "How the first-in-human study should monitor the organs the NOAEL rests "
                    "on: a proposal the medical monitor accepts, escalates or dismisses."
                ),
                current=self._recommendation_view(current),
                superseded=superseded,
            )
        if not any(v.current for v in self._noael_views(study_id)):
            return TranslationalView(
                study_id=study_id,
                status="no_decision",
                why=(
                    "No NOAEL decision is recorded, so preclinical.translational_bridge has "
                    "nothing to map: it starts from the findings the NOAEL rests on."
                ),
                current=None,
                superseded=superseded,
            )
        return TranslationalView(
            study_id=study_id,
            status="not_run",
            why="A NOAEL decision is recorded; preclinical.translational_bridge has not run yet.",
            current=None,
            superseded=superseded,
        )

    def monitoring_brief(self, study_id: str) -> MonitoringBrief:
        if not visibility.can_see_dose_proposal(self.role):
            return MonitoringBrief(status="not_shown")
        current, _ = self._recommendations(study_id)
        if current is None:
            return MonitoringBrief(status="none")
        view = self._recommendation_view(current)
        states: dict[str, Literal["open", "accepted", "escalated", "dismissed"]] = {
            "accepted": "accepted",
            "escalated": "escalated",
            "dismissed": "dismissed",
        }
        status = states.get(view.status, "open")
        return MonitoringBrief(
            status=status,
            finding_id=view.finding_id,
            organs=[i.label for i in view.items],
            unmapped=len(view.unmapped),
            decided_by=view.decided_by,
            decided_seq=view.decided_seq,
        )

    def dose_translation(self, study_id: str) -> DoseTranslationView:
        """The Dose translation panel: preclinical roles, as the rest of the study's page."""
        self._send_study(study_id)
        current, earlier = self._proposals(study_id)
        superseded = [r.finding_id for r in earlier]
        if current is not None:
            view = self._proposal_view(current)
            proposed = view.check == "dose_proposal"
            return DoseTranslationView(
                study_id=study_id,
                status="proposed" if proposed else "no_starting_dose",
                why=(
                    "The current NOAEL converted by FDA's 2005 method: a proposal the medical "
                    "monitor accepts, escalates or dismisses."
                    if proposed
                    else current.finding.title
                ),
                current=view,
                superseded=superseded,
            )
        decided = [v for v in self._noael_views(study_id) if v.current]
        if not decided:
            return DoseTranslationView(
                study_id=study_id,
                status="no_decision",
                why=(
                    "No NOAEL decision is recorded for this study, so "
                    "preclinical.dose_translation records nothing: it starts from the "
                    "toxicologist's NOAEL and never sets one."
                ),
                current=None,
                superseded=superseded,
            )
        return DoseTranslationView(
            study_id=study_id,
            status="not_run",
            why=(
                "A NOAEL decision is recorded, but preclinical.dose_translation has not run on "
                "it yet (the next agents run will)."
            ),
            current=None,
            superseded=superseded,
        )

    def starting_dose(self, study_id: str) -> StartingDose:
        """The planned first human dose from a preclinical study: the current proposal once
        accepted, with the chain from the study to the acceptance; or why there is none."""
        if not visibility.can_see_dose_proposal(self.role):
            return StartingDose(status="not_shown", why="not shown to your role")
        current, earlier = self._proposals(study_id)
        if current is None and not visibility.can_see_preclinical(self.role):
            # The medical monitor learns of preclinical work through a proposal only: with
            # none, not even whether a NOAEL is recorded.
            return StartingDose(status="no_proposal", why="No dose proposal has been raised yet.")
        noael = {v.decision_id: v for v in self._noael_views(study_id)}
        chain: list[ChainLink] = []
        load = self._send_loads().get(study_id)
        if load is not None and isinstance(load.payload, SendStudyLoaded):
            chain.append(
                ChainLink(
                    step="study",
                    label=f"{study_id}, SEND cut {load.payload.cut_id}",
                    ref=study_id,
                    who=load.actor,
                    seq=load.seq,
                )
            )
        if current is None:
            if not any(v.current for v in noael.values()):
                return StartingDose(
                    status="no_decision",
                    why=f"No NOAEL decision is recorded for {study_id}.",
                    chain=chain,
                )
            return StartingDose(
                status="no_proposal",
                why="The NOAEL is recorded; preclinical.dose_translation has not run on it yet.",
                chain=chain,
            )
        view = self._proposal_view(current)
        basis = noael.get(view.basis_decision_id or "")
        if basis is not None:
            dose = (
                f"NOAEL {sig(basis.noael_dose)} {basis.dose_unit}"
                if basis.noael_dose is not None
                else "NOAEL not established"
            )
            chain.append(
                ChainLink(
                    step="noael",
                    label=f"{dose} ({basis.sex}), {basis.decision_id}",
                    ref=basis.decision_id,
                    who=basis.decided_by,
                    seq=basis.seq,
                )
            )
        chain.append(
            ChainLink(
                step="proposal",
                label=view.title,
                ref=view.finding_id,
                who=current.finding.agent_id,
                seq=view.seq,
            )
        )
        if view.decided_by is not None and view.decided_seq is not None:
            chain.append(
                ChainLink(
                    step="decision",
                    label=f"{view.status} by {view.decided_by}",
                    ref=view.finding_id,
                    who=view.decided_by,
                    seq=view.decided_seq,
                )
            )
        values = {s.statistic: s.value for s in view.steps}
        dose_mg = values.get("mrsd:starting_dose_mg")
        mrsd = values.get("mrsd:mrsd_mg_per_kg")
        weight_kg = (dose_mg / mrsd) if dose_mg is not None and mrsd else None
        if view.check != "dose_proposal":
            return StartingDose(status="no_starting_dose", why=view.title, chain=chain)
        earlier_accepted = [
            r for r in earlier if self.snapshot().decided(r.finding_id) == "accepted"
        ]
        if view.status == "accepted":
            return StartingDose(
                status="planned",
                dose_mg=dose_mg,
                mrsd_mg_per_kg=mrsd,
                human_weight_kg=weight_kg,
                why=f"Accepted by {view.decided_by} (ledger seq {view.decided_seq}).",
                chain=chain,
            )
        if view.status == "dismissed":
            return StartingDose(
                status="dismissed",
                dose_mg=dose_mg,
                mrsd_mg_per_kg=mrsd,
                human_weight_kg=weight_kg,
                why=f"Dismissed by {view.decided_by}: {view.decision_reason or ''}".strip(),
                chain=chain,
            )
        waiting = (
            f"{view.finding_id} awaits the medical monitor's decision"
            if view.status != "escalated"
            else f"{view.finding_id} was escalated by {view.decided_by}"
        )
        if earlier_accepted:
            waiting += (
                f"; the accepted {earlier_accepted[-1].finding_id} was superseded on a revised "
                "NOAEL, so no starting dose is planned until this one is decided"
            )
        return StartingDose(
            status="escalated" if view.status == "escalated" else "proposed",
            dose_mg=dose_mg,
            mrsd_mg_per_kg=mrsd,
            human_weight_kg=weight_kg,
            why=waiting + ".",
            chain=chain,
        )

    def ind_readiness(self, asset_id: str) -> IndReadinessView:
        """The IND readiness checklist of a preclinical asset, from its newest inventory
        load and the agent's own rules; each gap links to the agent's current finding."""
        self._require_preclinical()
        if asset_id not in self._preclinical_assets or self._ind_config is None:
            raise NotFoundError(f"no IND readiness for {asset_id}")
        cfg = self._ind_config
        snap = self.snapshot()
        loads = [(seq, cut) for seq, (a, cut) in snap.inventory_loads.items() if a == asset_id]
        empty = IndReadinessView(
            asset_id=asset_id,
            status="not_loaded",
            cut_id=None,
            loaded_seq=None,
            planned_submission_date=cfg.planned_submission_date.isoformat(),
            planned_clinical_duration_days=cfg.planned_clinical_duration_days,
            multiple_dose=cfg.multiple_dose,
            percent_met=None,
            met=0,
            applicable=0,
            requirements=[],
        )
        if not loads or self._inventory_reader is None:
            return empty
        seq, cut = max(loads)
        try:
            frame = self._inventory_reader(asset_id, cut).table(INVENTORY_TABLE)
        except (OSError, StudyAccessError, MissingTableError) as exc:
            raise NotFoundError(f"inventory {asset_id}/{cut} is not on this host") from exc
        statuses = readiness.assess(readiness.studies_from(frame), cfg)
        by_check = {
            r.check: fid
            for fid, r in snap.raised.items()
            if r.finding.kind is FindingKind.IND_READINESS
            and r.finding.study_id == asset_id
            and snap.current(fid)
        }
        rows = []
        for st in statuses:
            s = st.study
            rows.append(
                RequirementView(
                    id=st.requirement.id,
                    title=st.requirement.title,
                    source=st.requirement.source,
                    section=st.requirement.section,
                    quote=st.requirement.quote,
                    note=st.requirement.note,
                    status=st.status,  # type: ignore[arg-type]
                    required_weeks=st.required_weeks,
                    candidates=len(st.candidates),
                    study_id=None if s is None else s.study_id,
                    study_title=None if s is None else s.title,
                    species=None if s is None else s.species,
                    duration_weeks=None if s is None else s.duration_weeks,
                    glp=None if s is None else s.glp,
                    report_status=None if s is None else s.report_status,
                    qa_audit_date=None
                    if s is None or s.qa_audit_date is None
                    else s.qa_audit_date.isoformat(),
                    problems=[c.split(":", 1)[0] for c in st.checks()],
                    finding_ids=[by_check[c] for c in st.checks() if c in by_check],
                )
            )
        applicable = [st for st in statuses if st.applicable]
        return empty.model_copy(
            update={
                "status": "assessed",
                "cut_id": cut,
                "loaded_seq": seq,
                "percent_met": round(readiness.percent_met(statuses), 1),
                "met": sum(st.met for st in applicable),
                "applicable": len(applicable),
                "requirements": rows,
            }
        )

    # ---- public safety signals (phase 5 step 3a)

    def safety_signals(self) -> SafetySignalsView:
        """The latest completed scan of openFDA for the study's drug: every tested term,
        the signals with their findings, and the label comparison. Shown to the scout's
        roles (the medical monitor and the portfolio lead)."""
        from meridian.agents.portfolio.safety_signal_scout import (
            AGENT_ID,
            TIERS,
            analyse,
            responses_from,
        )
        from meridian.portfolio import openfda as o

        cfg = self._signal_config
        if cfg is None:
            raise NotFoundError("the signal scout is not configured")
        if self.role not in set(cfg.visible_to):
            raise ForbiddenError(f"{self.role.value} does not see the safety signals")
        snap = self.snapshot()
        done = sorted(
            (
                e.payload.trigger_seq
                for e in snap.suspended
                if isinstance(e.payload, ActorSuspended)
                and e.payload.agent_id == AGENT_ID
                and e.payload.trigger_seq in snap.scans
            ),
        )
        rule = (
            f"Evans et al. 2001: at least {cfg.min_reports} reports, PRR at least "
            f"{cfg.prr_min:g}, chi-squared (Yates) at least {cfg.chi2_min:g}"
        )
        empty = SafetySignalsView(
            study_id=cfg.study_id,
            drug=cfg.drug,
            status="not_scanned",
            scan_id=None,
            scan_seq=None,
            data_date=None,
            product_query=f'{cfg.product_field}:"{cfg.product_term}"',
            drug_reports=None,
            all_reports=None,
            terms_with_drug=None,
            tested=0,
            signals=[],
            not_signals=[],
            label_set_id=None,
            label_version=None,
            label_effective=None,
            signals_listed=0,
            label_most_common=[],
            rule=rule,
            caveats=list(cfg.caveats),
            caveats_source=cfg.caveats_source,
            requests=0,
        )
        version = {
            e.payload.trigger_seq: e.payload.agent_version
            for e in snap.suspended
            if isinstance(e.payload, ActorSuspended) and e.payload.agent_id == AGENT_ID
        }

        def summary(d: int) -> ScanSummary:
            mine = [
                r
                for r in snap.raised.values()
                if r.finding.agent_id == AGENT_ID and r.cut_id == snap.scans[d]
            ]
            req = snap.scan_events[d]
            return ScanSummary(
                scan_seq=d,
                scan_id=snap.scans[d],
                agent_version=version.get(d),
                requested_by=req.requested_by,
                reuses_responses_of=req.reuses_responses_of,
                findings=len(mine),
                # In the queue: no closing decision, not superseded, not retired.
                open=sum(
                    1
                    for r in mine
                    if snap.status(r.finding_id) == "open"
                    and r.finding_id not in snap.superseded_by
                ),
                corrections=[correction_view(c) for c in snap.corrections.get(d, [])],
            )

        summaries = {d: summary(d) for d in done}
        triage = (
            "Only priority signals are findings: lower 95% limit of the PRR above "
            f"{cfg.priority_prr_low_above:g}, at least {cfg.priority_min_reports} reports, not "
            "an indication term, not a use term, not in the label. The others stay here with "
            "their tier (docs/signal-scout.md, 1.0.1)."
        )
        if not done:
            return empty
        seq = done[-1]
        scan_id = snap.scans[seq]
        frame = None if self._signal_responses is None else self._signal_responses(seq)
        if frame is None:
            return empty.model_copy(
                update={
                    "status": "responses_missing",
                    "scan_id": scan_id,
                    "scan_seq": seq,
                    "scan": summaries[seq],
                }
            )
        scan = analyse(responses_from(frame), cfg)
        by_term = {
            r.check.split(":", 1)[1]: r
            for r in snap.raised.values()
            if r.finding.agent_id == AGENT_ID and r.cut_id == scan_id
        }

        def row(t: object) -> SignalRow:
            from meridian.agents.portfolio.safety_signal_scout import TermResult

            assert isinstance(t, TermResult)
            raised = by_term.get(t.term)
            return SignalRow(
                term=t.term,
                reports=t.table.a,
                drug_reports=t.table.a + t.table.b,
                term_reports=t.table.a + t.table.c,
                prr=None if t.prr is None else t.prr.value,
                prr_low=None if t.prr is None else t.prr.low,
                prr_high=None if t.prr is None else t.prr.high,
                ror=None if t.ror is None else t.ror.value,
                ror_low=None if t.ror is None else t.ror.low,
                ror_high=None if t.ror is None else t.ror.high,
                chi2=t.chi2,
                signal=t.signal,
                label_listed=t.listed,
                finding_id=None if raised is None else raised.finding_id,
                status=None if raised is None else snap.status(raised.finding_id),
                tier=t.tier,
            )

        signals = [row(t) for t in scan.tested if t.signal]
        signals.sort(key=lambda r: -(r.prr or 0.0))
        tiers: dict[str, int] = {t: sum(1 for r in signals if r.tier == t) for t in TIERS}
        common = [
            LabelReaction(
                reaction=reaction,
                signal_terms=[
                    r.term for r in signals if o.normalise(r.term) == o.normalise(reaction)
                ],
            )
            for reaction in (scan.label.most_common if scan.label else ())
        ]
        return SafetySignalsView(
            study_id=cfg.study_id,
            drug=cfg.drug,
            status="scanned",
            scan_id=scan_id,
            scan_seq=seq,
            data_date=scan.data_date,
            product_query=f'{cfg.product_field}:"{cfg.product_term}"',
            drug_reports=scan.drug_reports,
            all_reports=scan.all_reports,
            terms_with_drug=scan.terms_with_drug,
            tested=len(scan.tested),
            not_answered=list(scan.not_answered),
            signals=signals,
            not_signals=[row(t) for t in scan.tested if not t.signal],
            label_set_id=None if scan.label is None else scan.label.set_id,
            label_version=None if scan.label is None else scan.label.version,
            label_effective=None if scan.label is None else scan.label.effective_time,
            signals_listed=sum(1 for r in signals if r.label_listed),
            label_most_common=common,
            rule=rule,
            caveats=list(cfg.caveats),
            caveats_source=cfg.caveats_source,
            requests=frame.height,
            scan=summaries[seq],
            earlier_scans=[summaries[d] for d in reversed(done[:-1])],
            tiers=tiers,
            triage=triage,
            context=list(cfg.context),
        )

    # ---- the portfolio's reviews and gates (phase 5 step 3b)

    def _require_lead(self, what: str) -> None:
        if self.role is not Role.PORTFOLIO_LEAD:
            raise ForbiddenError(f"only the portfolio lead {what}")

    def _require_portfolio_reader(self) -> None:
        if self.role not in visibility.PORTFOLIO_ROLES:
            raise ForbiddenError(
                f"{self.role.value} does not see the portfolio's reviews and gates"
            )

    def request_review(self, body: ReviewRequest) -> Recorded:
        """The portfolio lead asks for an outlook (PortfolioReviewRequested, reason requested)."""
        self._require_lead("asks for a portfolio review")
        now = self._clock.now()
        review_id = REVIEW_PREFIX + _stamp(now)
        if any(
            isinstance(e.payload, PortfolioReviewRequested) and e.payload.review_id == review_id
            for e in self._portfolio_events()
        ):
            raise ConflictError(f"{review_id} is already recorded: ask again in a second")
        event = self._ledger.append(
            EventDraft(
                occurred_at=now,
                actor=self.person.actor,
                visibility=Visibility.BLINDED,
                payload=PortfolioReviewRequested(
                    review_id=review_id,
                    requested_by=self.person.actor,
                    reason="requested",
                    note=body.note.strip() if body.note else None,
                ),
            )
        )
        self._snapshot = None
        return Recorded(seq=event.seq, id=review_id)

    def request_gate(self, body: GateRequestBody) -> Recorded:
        """The portfolio lead asks for the evidence pack of a gate of config/gates.toml."""
        self._require_lead("asks for a gate's evidence pack")
        if self._gates is None or self._gates.get(body.asset_id, body.gate) is None:
            raise InvalidDecisionError(
                f"no gate {body.gate} for {body.asset_id} in config/gates.toml"
            )
        now = self._clock.now()
        request_id = GATE_PREFIX + _stamp(now)
        if any(
            isinstance(e.payload, GateRequest) and e.payload.gate_request_id == request_id
            for e in self._portfolio_events()
        ):
            raise ConflictError(f"{request_id} is already recorded: ask again in a second")
        event = self._ledger.append(
            EventDraft(
                occurred_at=now,
                actor=self.person.actor,
                study_id=body.asset_id,
                visibility=Visibility.BLINDED,
                payload=GateRequest(
                    gate_request_id=request_id,
                    asset_id=body.asset_id,
                    gate=body.gate,
                    requested_by=self.person.actor,
                    note=body.note.strip() if body.note else None,
                ),
            )
        )
        self._snapshot = None
        return Recorded(seq=event.seq, id=request_id)

    def _portfolio_events(self) -> list[Event]:
        return [
            e
            for e in self._ledger.read(
                event_types=[
                    EventType.PORTFOLIO_REVIEW_REQUESTED,
                    EventType.PORTFOLIO_OUTLOOK,
                    EventType.GATE_REQUEST,
                    EventType.GATE_PACK,
                    EventType.GATE_DECISION,
                ],
                viewer_role=visibility.ledger_viewer(self.role),
            )
            if isinstance(e, Event)
        ]

    def outlook(self) -> OutlookView:
        self._require_portfolio_reader()
        events = self._portfolio_events()
        outlooks = [e for e in events if isinstance(e.payload, PortfolioOutlook)]
        answered = {
            e.payload.trigger_seq for e in outlooks if isinstance(e.payload, PortfolioOutlook)
        }
        pending = [
            e.seq
            for e in events
            if isinstance(e.payload, PortfolioReviewRequested) and e.seq not in answered
        ]
        if not outlooks:
            return OutlookView(status="none", pending_reviews=pending)
        last = outlooks[-1]
        assert isinstance(last.payload, PortfolioOutlook)
        return OutlookView(
            status="outlook",
            seq=last.seq,
            recorded_at=last.recorded_at,
            outlook=for_display(last.payload),
            pending_reviews=pending,
        )

    def gates(self) -> GatesView:
        self._require_portfolio_reader()
        events = self._portfolio_events()
        packs: dict[int, tuple[int, GatePack]] = {}
        decisions: dict[int, GateDecisionView] = {}
        for e in events:
            if isinstance(e.payload, GatePack):
                packs[e.payload.trigger_seq] = (e.seq, e.payload)
            elif isinstance(e.payload, GateDecision):
                decisions[e.payload.gate_request_seq] = GateDecisionView(
                    seq=e.seq, recorded_at=e.recorded_at, decision=e.payload
                )
        requests = []
        for e in reversed(events):
            if not isinstance(e.payload, GateRequest):
                continue
            pack = packs.get(e.seq)
            requests.append(
                GateRequestView(
                    seq=e.seq,
                    recorded_at=e.recorded_at,
                    request=e.payload,
                    pack_seq=None if pack is None else pack[0],
                    pack=None if pack is None else pack_for_display(pack[1]),
                    decision=decisions.get(e.seq),
                )
            )
        defs = [
            GateDef(
                asset_id=g.asset_id,
                gate=g.id,
                title=g.title,
                criteria=[
                    GateCriterionDef(
                        id=c.id, title=c.title, basis=c.basis, source=c.source, section=c.section
                    )
                    for c in g.criteria
                ],
            )
            for g in (self._gates.gates if self._gates else ())
        ]
        return GatesView(
            gates=defs, requests=requests, can_request=self.role is Role.PORTFOLIO_LEAD
        )

    def decide_gate(self, request_seq: int, body: GateDecisionBody) -> GateDecisionView:
        """The portfolio lead's gate decision (GateDecision) on the newest evidence pack of a
        gate request. Once per request: a new decision needs a new request and pack."""
        self._require_lead("records a gate decision")
        view = next((r for r in self.gates().requests if r.seq == request_seq), None)
        if view is None:
            raise NotFoundError(f"no gate request at seq {request_seq}")
        if view.pack_seq is None:
            raise ConflictError("the gate request has no evidence pack yet: run the agents first")
        if body.pack_seq != view.pack_seq:
            raise ConflictError(f"the evidence pack of this request is seq {view.pack_seq}")
        if view.decision is not None:
            raise ConflictError(f"the gate request is already decided (seq {view.decision.seq})")
        try:
            payload = GateDecision(
                gate_request_seq=request_seq,
                pack_seq=view.pack_seq,
                asset_id=view.request.asset_id,
                gate=view.request.gate,
                decision=body.decision,
                conditions=tuple(c.strip() for c in body.conditions if c.strip()),
                rationale=body.rationale.strip(),
                decided_by=self.person.actor,
            )
        except ValidationError as exc:
            raise InvalidDecisionError(
                "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
            ) from exc
        event = self._ledger.append(
            EventDraft(
                occurred_at=self._clock.now(),
                actor=self.person.actor,
                study_id=view.request.asset_id,
                visibility=Visibility.BLINDED,
                payload=payload,
            )
        )
        self._snapshot = None
        return GateDecisionView(seq=event.seq, recorded_at=event.recorded_at, decision=payload)

    # ---- deciding

    def decide(
        self, finding_id: str, action: DecisionAction, reason: str | None
    ) -> DecisionResponse:
        r = self._visible(finding_id)  # 404 before anything else: existence is not leaked
        blocked = self._blocked(r.finding)
        if blocked is not None:
            raise ForbiddenError(blocked)
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


def _started(run: _Run) -> datetime:
    """When a run started: ActorWoke.started_at (the parallel runner appends ActorWoke after
    the run), or the event's time on older events, appended as the run started."""
    return _woke(run).started_at or run.woke.recorded_at


def _trigger(e: Event) -> int | None:
    p = e.payload
    return p.trigger_seq if isinstance(p, ActorWoke | ActorSuspended) else None


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _stamp(clock_now: datetime) -> str:
    return clock_now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
