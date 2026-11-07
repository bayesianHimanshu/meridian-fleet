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
from meridian.data.study_store import ParquetStudyReader, StudyReader
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

    def study_reader(self, cut_id: str) -> StudyReader:
        return ParquetStudyReader(self.curated_root / self.study_id, cut_id)


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
        )
    finally:
        ledger.close()


Caller = Annotated[Person, Depends(caller)]
Repo = Annotated[Repository, Depends(repository)]
Context = Annotated[ApiContext, Depends(context)]
