"""The Fleet API application: routes, error mapping, CORS and the development-auth header."""

import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from meridian.agents.catalog import build_registry
from meridian.api.deps import ApiContext, caller
from meridian.api.identity import ACTOR_HEADER, AUTH_HEADER, AUTH_MODE, ROLE_HEADER, People
from meridian.api.models import AssetStatic, AssetStatus
from meridian.api.repository import (
    ConflictError,
    ForbiddenError,
    InvalidDecisionError,
    NotFoundError,
    PortfolioEntry,
)
from meridian.api.routes.core import public, router
from meridian.platform.clock import Clock
from meridian.platform.config import Settings

HOST = "127.0.0.1"  # the API binds to the loopback interface only
DEFAULT_PORT = 8765
CORS_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")  # the Vite dev server
VERSION = "1.0.0"
DESCRIPTION = f"""\
The Meridian Fleet API: read findings, narratives, the ledger and the fleet, and record
human decisions. Agents raise findings; people close them here. Nothing is ever updated
or deleted: a decision is an appended HumanDecision event.

**This is not real authentication.** Every request names a person in `{ACTOR_HEADER}`
and a role in `{ROLE_HEADER}`; the API only checks that config/people.toml lists that
person with that role. Anyone who can reach the port can claim any listed identity. Every
response carries `{AUTH_HEADER}: {AUTH_MODE}`. Development use only, on 127.0.0.1.
"""


def load_portfolio(path: Path) -> tuple[PortfolioEntry, ...]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)["assets"]
    return tuple(
        PortfolioEntry(
            asset_id=a["asset_id"],
            static=AssetStatic(
                title=a["title"],
                stage=a["stage"],
                indication=a["indication"],
                intervention=a.get("intervention"),
                milestone=a["milestone"],
                milestone_due=a["milestone_due"],
                progress_percent=int(a["progress_percent"]),
                probability_of_success=a.get("probability_of_success"),
                status=AssetStatus(level=a["status_level"], text=a["status_text"]),
            ),
            live_study=bool(a.get("live_study", False)),
        )
        for a in raw
    )


def context_from_settings(
    settings: Settings, clock: Clock, people_file: Path, portfolio_file: Path
) -> ApiContext:
    return ApiContext(
        ledger_path=settings.ledger_path,
        finding_id_prefixes=dict(settings.finding_id_prefixes),
        clock=clock,
        study_id=settings.study_id,
        people=People.load(people_file),
        registry=build_registry(settings.agents),
        agents=settings.agents,
        policies_dir=settings.runtime.policies_dir,
        ground_truth_dir=settings.ground_truth_dir,
        curated_root=settings.curated_root,
        portfolio=load_portfolio(portfolio_file),
        operational_cuts=tuple(settings.cuts.operational),
        safety_root=settings.safety_root,
    )


def create_app(ctx: ApiContext) -> FastAPI:
    app = FastAPI(
        title="Meridian Fleet API",
        version=VERSION,
        description=DESCRIPTION,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.context = ctx
    app.include_router(public)
    app.include_router(router, dependencies=[Depends(caller)])

    errors: dict[type[Exception], int] = {
        NotFoundError: 404,
        ForbiddenError: 403,
        ConflictError: 409,
        InvalidDecisionError: 422,
    }
    for error_type, status in errors.items():

        async def handle(request: Request, exc: Exception, status: int = status) -> JSONResponse:
            return JSONResponse(status_code=status, content={"detail": str(exc)})

        app.add_exception_handler(error_type, handle)

    @app.middleware("http")
    async def development_auth_header(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers[AUTH_HEADER] = AUTH_MODE
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CORS_ORIGINS),
        allow_methods=["GET", "POST"],
        allow_headers=[ACTOR_HEADER, ROLE_HEADER, "Content-Type"],
        expose_headers=[AUTH_HEADER],
    )
    return app
