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

import polars as pl
from fastapi import Depends, Header, HTTPException, Request

from meridian.agents.registry import AgentRegistry
from meridian.api.identity import ACTOR_HEADER, ROLE_HEADER, People, Person
from meridian.api.repository import PortfolioEntry, Repository
from meridian.data.study_store import (
    InventoryStageReader,
    LayeredStudyReader,
    ParquetStudyReader,
    SendStudyReader,
    StudyReader,
    UnblindedReader,
)
from meridian.domain.safety import SAFETY_CASES, SAFETY_UNBLINDED
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
    etmf_root: Path = Path("data/etmf")
    run_timeout_seconds: int = 120  # [runtime] exec_timeout_seconds
    # Policy to executor ([runtime] executors when [runtime] executor is zoned); a policy
    # not listed runs on default_executor.
    executors: tuple[tuple[str, str], ...] = ()
    default_executor: str = "openshell"
    # Phase 5: SEND studies under <send_root>/<study>/<cut>, and preclinical asset id to
    # its SEND study id ([preclinical] asset_id and study_id).
    send_root: Path = Path("data/send")
    preclinical_assets: tuple[tuple[str, str], ...] = ()
    # Phase 5 step 2: inventory cuts under <inventory_root>/<asset>/<cut>/inventory.
    inventory_root: Path = Path("data/inventory_cuts")
    # Phase 5 step 3a: each signal scan's kept openFDA responses.
    signal_root: Path = Path("var/signals")

    def signal_responses(self, trigger_seq: int) -> pl.DataFrame | None:
        from meridian.portfolio.store import kept

        return kept(self.signal_root, trigger_seq)

    def send_reader(self, study_id: str, cut_id: str) -> StudyReader:
        return SendStudyReader(self.send_root / study_id, cut_id)

    def inventory_reader(self, asset_id: str, cut_id: str) -> StudyReader:
        return InventoryStageReader.at_cut_dir(
            self.inventory_root / asset_id / cut_id, cut_id=cut_id, study_id=asset_id
        )

    def tmf_reader(self, cut_id: str) -> StudyReader:
        """Blinded DM of the cut, layered with its eTMF snapshot (index, visits, export)."""
        return LayeredStudyReader(
            ParquetStudyReader(self.curated_root / self.study_id, cut_id),
            ParquetStudyReader(self.etmf_root / self.study_id, cut_id),
        )

    def study_reader(self, cut_id: str) -> StudyReader:
        return ParquetStudyReader(self.curated_root / self.study_id, cut_id)

    def safety_case_counts(self, cut_id: str) -> tuple[int, int]:
        """(cases, cases with an unblinded treatment field) of one cut's safety database.
        Reads only row counts; no treatment value leaves this function."""
        root = self.safety_root / self.study_id
        cases = ParquetStudyReader(root, cut_id).table(SAFETY_CASES).height
        with_treatment = UnblindedReader(root, cut_id).table(SAFETY_UNBLINDED).height
        return cases, with_treatment


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


def make_repository(ctx: ApiContext, person: Person, ledger: SqliteLedger) -> Repository:
    """The Repository of one person over one open ledger (also the host's portfolio facts
    builder, phase 5 step 3b, which acts as the portfolio lead)."""
    return Repository(
        ledger=ledger,
        person=person,
        clock=ctx.clock,
        study_id=ctx.study_id,
        registry=ctx.registry,
        policies_dir=ctx.policies_dir,
        ground_truth_dir=ctx.ground_truth_dir,
        portfolio=ctx.portfolio,
        operational_cuts=ctx.operational_cuts,
        run_timeout_seconds=ctx.run_timeout_seconds,
        executors=dict(ctx.executors),
        default_executor=ctx.default_executor,
        send_reader=ctx.send_reader,
        tox_config=ctx.agents.tox_study_monitor,
        preclinical_assets=dict(ctx.preclinical_assets),
        inventory_reader=ctx.inventory_reader,
        ind_config=ctx.agents.ind_readiness,
        signal_responses=ctx.signal_responses,
        signal_config=ctx.agents.safety_signal_scout,
        gates=ctx.agents.stage_gate_pack.gates,
    )


def repository(
    ctx: Annotated[ApiContext, Depends(context)],
    person: Annotated[Person, Depends(caller)],
) -> Iterator[Repository]:
    ledger = SqliteLedger(
        path=ctx.ledger_path, clock=ctx.clock, finding_id_prefixes=ctx.finding_id_prefixes
    )
    try:
        yield make_repository(ctx, person, ledger)
    finally:
        ledger.close()


Caller = Annotated[Person, Depends(caller)]
Repo = Annotated[Repository, Depends(repository)]
Context = Annotated[ApiContext, Depends(context)]
