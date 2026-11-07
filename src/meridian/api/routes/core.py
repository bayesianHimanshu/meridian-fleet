"""Every /api/v1 route. Each handler is one Repository call."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from meridian.api.deps import Caller, Context, Repo
from meridian.api.models import (
    CutSummary,
    DecisionRequest,
    DecisionResponse,
    Enrollment,
    FindingDetail,
    FindingPage,
    Fleet,
    Health,
    LedgerPage,
    PersonView,
    Portfolio,
    RbqmSites,
    ReviewQueue,
    StudySummary,
    VerifyReport,
    WhoAmI,
)
from meridian.api.repository import LEDGER_PAGE_MAX, FindingFilters
from meridian.domain.enums import EventType, FindingKind, Severity

public = APIRouter(prefix="/api/v1")
router = APIRouter(prefix="/api/v1")

Status = Literal["open", "accepted", "escalated", "dismissed"]


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
    superseded findings; widen with scope=all and include_superseded=true."""
    filters = FindingFilters(
        scope=scope,
        include_superseded=include_superseded,
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
    repo: Repo, scope: Scope = "operational", include_superseded: bool = False
) -> ReviewQueue:
    """Open and escalated findings the caller may see, newest first. By default only
    operational cuts (never evaluation cuts) and no superseded findings."""
    return repo.review_queue(FindingFilters(scope=scope, include_superseded=include_superseded))


@router.get("/ledger", tags=["ledger"])
def ledger(
    repo: Repo,
    since: Annotated[int, Query(ge=0)] = 0,
    type: Annotated[list[EventType] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=LEDGER_PAGE_MAX)] = 100,
    order: Literal["asc", "desc"] = "asc",
    before: Annotated[int | None, Query(ge=1)] = None,
) -> LedgerPage:
    """Events as the caller may see them (restricted ones redacted). order=asc (default):
    after seq `since`, oldest first, continue with next_since. order=desc: newest first,
    before seq `before` if given, continue with next_before."""
    return repo.ledger_page(since, type, limit, order, before)


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


@router.get("/portfolio", tags=["portfolio"])
def portfolio(repo: Repo) -> Portfolio:
    return repo.portfolio()
