"""Every /api/v1 route. Each handler is one Repository call."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from meridian.api.deps import Caller, Context, Repo
from meridian.api.models import (
    AuditView,
    CutSummary,
    DecisionRequest,
    DecisionResponse,
    DoseTranslationView,
    Enrollment,
    FindingDetail,
    FindingPage,
    Fleet,
    GateDecisionBody,
    GateDecisionView,
    GateRequestBody,
    GatesView,
    Health,
    IndReadinessView,
    LedgerPage,
    NoaelDecisionView,
    NoaelHistory,
    NoaelRequest,
    ObservationItem,
    ObservationRequest,
    ObservationResponse,
    OutlookView,
    PersonView,
    Portfolio,
    PreclinicalEndpoints,
    PreclinicalStudy,
    RbqmSites,
    Reconciliation,
    Recorded,
    ReviewQueue,
    ReviewRequest,
    SafetySignalsView,
    StudySummary,
    TmfCompletenessView,
    TranslationalView,
    UnblindedCases,
    VerifyReport,
    WhoAmI,
)
from meridian.api.repository import LEDGER_PAGE_MAX, FindingFilters
from meridian.domain.enums import EventType, FindingKind, Severity

public = APIRouter(prefix="/api/v1")
router = APIRouter(prefix="/api/v1")

Status = Literal["open", "accepted", "escalated", "dismissed", "retired"]


@public.get("/health", tags=["meta"])
def health() -> Health:
    """Liveness. The only route that needs no identity headers."""
    return Health()


@public.get("/people", tags=["meta"])
def people(ctx: Context) -> list[PersonView]:
    """The demo people of config/people.toml, for the development role switcher. Needs no
    identity headers: this is development identity, not sign-in."""
    return [  # in the file's order
        PersonView(actor=p.actor, name=p.name, role=p.role) for p in ctx.people.people.values()
    ]


@router.get("/whoami", tags=["meta"])
def whoami(person: Caller) -> WhoAmI:
    return WhoAmI(actor=person.actor, name=person.name, role=person.role)


@router.get("/studies", tags=["studies"])
def studies(repo: Repo) -> list[StudySummary]:
    return repo.studies()


@router.get("/studies/{study}/cuts", tags=["studies"])
def cuts(study: str, repo: Repo) -> list[CutSummary]:
    return repo.cuts(study)


Scope = Literal["operational", "evaluation", "all"]


@router.get("/studies/{study}/enrollment", tags=["studies"])
def study_enrollment(study: str, repo: Repo, ctx: Context, cut: str | None = None) -> Enrollment:
    """Cumulative treated subjects by month (blinded DM RFSTDTC), per site and in total.
    Default cut: the study's operational cut."""
    return repo.enrollment(study, cut, ctx.study_reader)


@router.get("/findings", tags=["findings"])
def findings(
    repo: Repo,
    scope: Scope = "operational",
    include_superseded: bool = False,
    include_retired: bool = False,
    include_previously_decided: bool = False,
    cut: str | None = None,
    agent: str | None = None,
    kind: FindingKind | None = None,
    site: str | None = None,
    severity: Severity | None = None,
    status: Status | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FindingPage:
    """Newest first (ledger seq descending). By default only operational cuts and no
    superseded, retired or previously decided findings; widen with scope=all and the
    include_ flags."""
    filters = FindingFilters(
        scope=scope,
        include_superseded=include_superseded,
        include_retired=include_retired,
        include_previously_decided=include_previously_decided,
        cut=cut,
        agent=agent,
        kind=None if kind is None else kind.value,
        site=site,
        severity=None if severity is None else severity.value,
        status=status,
    )
    return repo.findings(filters, limit, offset)


@router.get("/findings/{finding_id}", tags=["findings"])
def finding(finding_id: str, repo: Repo) -> FindingDetail:
    return repo.finding(finding_id)


@router.post("/findings/{finding_id}/decisions", tags=["findings"], status_code=201)
def decide(finding_id: str, body: DecisionRequest, repo: Repo) -> DecisionResponse:
    """Append one HumanDecision. 404 if the caller may not see the finding, 403 for
    qa_auditor, 422 for a dismissal without a reason, 409 if already accepted or dismissed."""
    return repo.decide(finding_id, body.action, body.reason)


@router.get("/review-queue", tags=["findings"])
def review_queue(
    repo: Repo,
    scope: Scope = "operational",
    include_superseded: bool = False,
    include_retired: bool = False,
    include_previously_decided: bool = False,
) -> ReviewQueue:
    """Open and escalated findings the caller may see, newest first. By default only
    operational cuts (never evaluation cuts), and no superseded, retired or previously
    decided findings (a person closed the same finding from an earlier agent version and
    its primary evidence is unchanged)."""
    return repo.review_queue(
        FindingFilters(
            scope=scope,
            include_superseded=include_superseded,
            include_retired=include_retired,
            include_previously_decided=include_previously_decided,
        )
    )


@router.get("/ledger", tags=["ledger"])
def ledger(
    repo: Repo,
    since: Annotated[int, Query(ge=0)] = 0,
    type: Annotated[list[EventType] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=LEDGER_PAGE_MAX)] = 100,
    order: Literal["asc", "desc"] = "asc",
    before: Annotated[int | None, Query(ge=1)] = None,
    agent: Annotated[str | None, Query(max_length=64)] = None,
) -> LedgerPage:
    """Events as the caller may see them (restricted ones redacted). order=asc (default):
    after seq `since`, oldest first, continue with next_since. order=desc: newest first,
    before seq `before` if given, continue with next_before. agent: only events the caller
    may read in full whose payload names that agent (redacted events never match)."""
    return repo.ledger_page(since, type, limit, order, before, agent)


@router.get("/ledger/verify", tags=["ledger"])
def ledger_verify(repo: Repo) -> VerifyReport:
    return repo.verify()


@router.get("/fleet", tags=["fleet"])
def fleet(repo: Repo) -> Fleet:
    return repo.fleet()


@router.get("/rbqm/{cut}/sites", tags=["rbqm"])
def rbqm_sites(cut: str, repo: Repo, ctx: Context) -> RbqmSites:
    """Per-site metrics for the RBQM heatmap (clinical ops, medical monitor, QA only)."""
    return repo.rbqm_sites(cut, ctx.study_reader, ctx.agents)


@router.get("/safety/{cut}/reconciliation", tags=["safety"])
def safety_reconciliation(cut: str, repo: Repo) -> Reconciliation:
    """SAE reconciliation of one cut: counts per check and site, and the discrepancies,
    from the findings the caller may see. Subject-level findings reach only the roles in
    their visible_to; for anyone else they are neither listed nor counted."""
    return repo.reconciliation(cut)


@router.get("/safety/unblinded-cases", tags=["safety"])
def safety_unblinded_cases(repo: Repo, ctx: Context) -> UnblindedCases:
    """Unblinded statistician only: how many cases carry a treatment field (a count)."""
    return repo.unblinded_cases(ctx.safety_case_counts)


@router.get("/quality/{cut}/tmf", tags=["quality"])
def quality_tmf(cut: str, repo: Repo, ctx: Context) -> TmfCompletenessView:
    """TMF completeness of one cut: completeness and timely filing per site and for the
    trial, the expected-against-filed matrix, and the current quality.tmf_completeness
    findings the caller may see. No subject data; the roles of the agent's config only."""
    return repo.tmf(cut, ctx.tmf_reader, ctx.agents.tmf_completeness)


@router.post("/quality/observations", tags=["quality"], status_code=201)
def raise_observation(body: ObservationRequest, repo: Repo) -> ObservationResponse:
    """QA auditor only: record an observation (a QualityObservation event and a
    qa_observation finding raised by the auditor). A finding id or ledger seq it refers to
    must exist and be visible to the auditor. Decided by clinical_ops_lead or
    medical_monitor, never by the auditor who raised it."""
    return repo.raise_observation(body)


@router.get("/quality/observations", tags=["quality"])
def observations(repo: Repo) -> list[ObservationItem]:
    """The QA observations the caller may see (qa_auditor, clinical_ops_lead, medical_monitor)."""
    return repo.observations()


@router.get("/quality/audit", tags=["quality"])
def quality_audit(repo: Repo, ctx: Context) -> AuditView:
    """The last audit trail review and the current audit findings (qa_auditor and
    clinical_ops_lead only)."""
    return repo.audit(ctx.agents.audit_trail_reviewer)


@router.get("/portfolio", tags=["portfolio"])
def portfolio(repo: Repo) -> Portfolio:
    return repo.portfolio()


# ---- preclinical (phase 5 step 1): toxicologist, portfolio lead and QA auditor only


@router.get("/preclinical/studies", tags=["preclinical"])
def preclinical_studies(repo: Repo) -> list[PreclinicalStudy]:
    """The SEND studies loaded (SendStudyLoaded) and their design: dose groups by phase,
    with the animals of each sex."""
    return repo.preclinical_studies()


@router.get("/preclinical/{study}/endpoints", tags=["preclinical"])
def preclinical_endpoints(study: str, repo: Repo) -> PreclinicalEndpoints:
    """Every endpoint by sex, phase and dose group against the concurrent control, with
    the tests and flags of preclinical.tox_study_monitor's analysis, the finding raised on
    it when the caller can see it, and per dose group the findings that flag it."""
    return repo.preclinical_endpoints(study)


@router.get("/preclinical/{study}/noael", tags=["preclinical"])
def preclinical_noael(study: str, repo: Repo) -> NoaelHistory:
    """The study's current NOAEL decision per sex (or combined) and every earlier one."""
    return repo.noael(study)


@router.post("/preclinical/{study}/noael", tags=["preclinical"], status_code=201)
def record_noael(study: str, body: NoaelRequest, repo: Repo) -> NoaelDecisionView:
    """Toxicologist only: record a NOAEL decision (NoaelDecision), append-only. The doses
    must be the study's; the findings must exist and be the study's; a revision names the
    current decision in supersedes (409 otherwise). The agent never proposes a NOAEL."""
    return repo.record_noael(study, body)


@router.get("/preclinical/{study}/dose-translation", tags=["preclinical"])
def preclinical_dose_translation(study: str, repo: Repo) -> DoseTranslationView:
    """The current dose proposal (preclinical.dose_translation) with its working step by
    step and its sources, or why there is none. A proposal, never a decision."""
    return repo.dose_translation(study)


@router.get("/preclinical/assets/{asset}/ind-readiness", tags=["preclinical"])
def preclinical_ind_readiness(asset: str, repo: Repo) -> IndReadinessView:
    """The first-in-human requirements checklist for an asset's nonclinical inventory, with
    each requirement's status and links to the gap findings (preclinical.ind_readiness)."""
    return repo.ind_readiness(asset)


@router.get("/preclinical/{study}/translational", tags=["preclinical"])
def preclinical_translational(study: str, repo: Repo) -> TranslationalView:
    """The current monitoring recommendation for the first-in-human study
    (preclinical.translational_bridge): per target organ what to measure and when, with
    its sources, and the safety margins. A proposal, never a decision."""
    return repo.translational(study)


@router.get("/safety/signals", tags=["safety"])
def safety_signals(repo: Repo) -> SafetySignalsView:
    """The latest scan of public FAERS data (openFDA) for MRD-201's drug: signals of
    disproportionate reporting with PRR, ROR and counts, the label comparison, the data
    date and FAERS's caveats (portfolio.safety_signal_scout). Never incidences or causes."""
    return repo.safety_signals()


# ---- the portfolio's reviews and gates (phase 5 step 3b)


@router.get("/portfolio/outlook", tags=["portfolio"])
def portfolio_outlook(repo: Repo) -> OutlookView:
    """The latest PortfolioOutlook (portfolio.pipeline_intelligence): per asset a status,
    the top risks with their ledger seqs, the facts and the changes since the previous
    outlook. Information only. The portfolio lead and the QA auditor."""
    return repo.outlook()


@router.post("/portfolio/reviews", tags=["portfolio"], status_code=201)
def portfolio_request_review(body: ReviewRequest, repo: Repo) -> Recorded:
    """Portfolio lead only: ask for an outlook (PortfolioReviewRequested). The agents answer
    it on their next run."""
    return repo.request_review(body)


@router.get("/portfolio/gates", tags=["portfolio"])
def portfolio_gates(repo: Repo) -> GatesView:
    """The gates of config/gates.toml and every gate request with its evidence pack and
    decision. The pack never says go or no-go. The portfolio lead and the QA auditor."""
    return repo.gates()


@router.post("/portfolio/gates", tags=["portfolio"], status_code=201)
def portfolio_request_gate(body: GateRequestBody, repo: Repo) -> Recorded:
    """Portfolio lead only: ask for a gate's evidence pack (GateRequest)."""
    return repo.request_gate(body)


@router.post("/portfolio/gates/{request_seq}/decision", tags=["portfolio"], status_code=201)
def portfolio_decide_gate(request_seq: int, body: GateDecisionBody, repo: Repo) -> GateDecisionView:
    """Portfolio lead only: go, conditional go (with conditions) or no-go on the request's
    evidence pack, with a rationale (GateDecision). Once per request (409 after)."""
    return repo.decide_gate(request_seq, body)
