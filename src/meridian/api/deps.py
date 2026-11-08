"""FastAPI dependencies: who is calling, and a Repository for them.

This is the only API module that opens the ledger. Each request gets its own SqliteLedger
(an SQLite connection belongs to the thread that made it, and FastAPI runs handlers in a
thread pool) and closes it afterwards. Route handlers receive a Repository and never see
the ledger store.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from meridian.agents.registry import AgentRegistry
from meridian.api.identity import ACTOR_HEADER, ROLE_HEADER, People, Person
from meridian.api.repository import PortfolioEntry, Repository
from meridian.data.study_store import ParquetStudyReader, StudyReader, UnblindedReader
from meridian.domain.safety import CASEID, SAFETY_CASES, SAFETY_UNBLINDED, USUBJID
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import Clock
from meridian.platform.config import AgentsSettings


@dataclass(frozen=True, slots=True)
class ApiContext:
    ledger_path: Path
    finding_id_prefixes: dict[str, str]
    clock: Clock
    study_id: str
    people: People
    registry: AgentRegistry
    agents: AgentsSettings
    policies_dir: Path
    ground_truth_dir: Path
    curated_root: Path
    portfolio: tuple[PortfolioEntry, ...]
    operational_cuts: tuple[str, ...] = ("DC-BASE",)
    safety_root: Path = Path("data/safety_db")

    def study_reader(self, cut_id: str) -> StudyReader:
        return ParquetStudyReader(self.curated_root / self.study_id, cut_id)

    def safety_case_counts(self, cut_id: str) -> tuple[int, int]:
        """(cases, cases with an unblinded treatment field) of one cut's safety database.
        Reads only row counts; no treatment value leaves this function."""
        root = self.safety_root / self.study_id
        cases = ParquetStudyReader(root, cut_id).table(SAFETY_CASES).height
        with_treatment = UnblindedReader(root, cut_id).table(SAFETY_UNBLINDED).height
        return cases, with_treatment

    def safety_case_subjects(self, cut_id: str) -> dict[str, str]:
        """CASEID to USUBJID of one cut's blinded safety case table: the subject of a
        case-anchored reconciliation finding (a late report) whose evidence names only the
        case. The caller only asks for findings the role may already see."""
        root = self.safety_root / self.study_id
        table = ParquetStudyReader(root, cut_id).table(SAFETY_CASES)
        return dict(table.select(CASEID, USUBJID).sort(CASEID).iter_rows())


def context(request: Request) -> ApiContext:
    ctx: ApiContext = request.app.state.context
    return ctx


def caller(
    ctx: Annotated[ApiContext, Depends(context)],
    actor: Annotated[
        str | None,
        Header(
            alias=ACTOR_HEADER, description="Person id from config/people.toml (development only)."
        ),
    ] = None,
    role: Annotated[
        str | None,
        Header(alias=ROLE_HEADER, description="That person's role (development only)."),
    ] = None,
) -> Person:
    """Fail closed: a missing header, an unknown person or another role is 401."""
    person = ctx.people.resolve(actor, role)
    if person is None:
        raise HTTPException(
            status_code=401,
            detail=f"unknown caller: send {ACTOR_HEADER} and {ROLE_HEADER} for a person in "
            "config/people.toml with that person's role (development identity)",
        )
    return person


def repository(
    ctx: Annotated[ApiContext, Depends(context)],
    person: Annotated[Person, Depends(caller)],
) -> Iterator[Repository]:
    ledger = SqliteLedger(
        path=ctx.ledger_path, clock=ctx.clock, finding_id_prefixes=ctx.finding_id_prefixes
    )
    try:
        yield Repository(
            ledger=ledger,
            person=person,
            clock=ctx.clock,
            study_id=ctx.study_id,
            registry=ctx.registry,
            policies_dir=ctx.policies_dir,
            ground_truth_dir=ctx.ground_truth_dir,
            portfolio=ctx.portfolio,
            operational_cuts=ctx.operational_cuts,
        )
    finally:
        ledger.close()


Caller = Annotated[Person, Depends(caller)]
Repo = Annotated[Repository, Depends(repository)]
Context = Annotated[ApiContext, Depends(context)]
