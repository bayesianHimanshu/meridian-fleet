"""Every /api/v1 route. Each handler is one Repository call."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from meridian.api.deps import Caller, Context, Repo
from meridian.api.models import (
    CutSummary,
    DecisionRequest,
    DecisionResponse,
    FindingDetail,
    FindingPage,
    Fleet,
    Health,
    LedgerPage,
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


@router.get("/whoami", tags=["meta"])
def whoami(person: Caller) -> WhoAmI:
    return WhoAmI(actor=person.actor, name=person.name, role=person.role)


@router.get("/studies", tags=["studies"])
def studies(repo: Repo) -> list[StudySummary]:
    return repo.studies()


@router.get("/studies/{study}/cuts", tags=["studies"])
def cuts(study: str, repo: Repo) -> list[CutSummary]:
    return repo.cuts(study)


@router.get("/findings", tags=["findings"])
def findings(
    repo: Repo,
    cut: str | None = None,
    agent: str | None = None,
    kind: FindingKind | None = None,
    site: str | None = None,
    severity: Severity | None = None,
    status: Status | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FindingPage:
    """Newest first (ledger seq descending)."""
    filters = FindingFilters(
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
def review_queue(repo: Repo) -> ReviewQueue:
    """Open and escalated findings the caller may see, newest first."""
    return repo.review_queue()


@router.get("/ledger", tags=["ledger"])
def ledger(
    repo: Repo,
    since: Annotated[int, Query(ge=0)] = 0,
    type: Annotated[list[EventType] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=LEDGER_PAGE_MAX)] = 100,
) -> LedgerPage:
    """Events after seq `since`, as the caller may see them (restricted ones redacted)."""
    return repo.ledger_page(since, type, limit)


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
