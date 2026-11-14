"""The wire between the laptop runner and the in-cluster dispatcher (phase 4b step 2).

The laptop reaches the dispatcher through one long-lived `kubectl port-forward`. Both ends
hold the same random token (the laptop in a 0600 file outside the repository, the
dispatcher in a Kubernetes Secret mounted read-only); it is never logged or sent in clear
outside that tunnel. Authentication goes both ways:

- the runner sends `Authorization: Bearer <token>` and a fresh random nonce; the
  dispatcher refuses anything else with 401 before reading the body;
- the dispatcher answers with X-Meridian-Dispatcher-Proof, an HMAC-SHA256 under the token
  of the nonce and the sha256 of the response body, so the runner knows the answer came
  from a holder of the token and was not altered. A response without a valid proof is
  refused.

Endpoints (all JSON, all but /healthz authenticated):

    GET  /healthz                    200 "ok" (the readiness probe; no data)
    GET  /v1/templates/<name>        the deployed ActorTemplate, as `kubectl ate get`
    GET  /v1/tags/<name>             a Tag
    GET  /v1/actors                  {"actors": [...]} in the atespace
    GET  /v1/capacity                {"free_workers": n}
    POST /v1/runs                    one run (headers below, body the run's tar)
    DELETE /v1/actors/<name>         delete one actor (cleanup after a lost answer)

A run request carries X-Meridian-Agent, X-Meridian-Archive-Sha256 (the worker's own
headers), X-Meridian-Template and X-Meridian-Actor. The answer is a RunAnswer.
"""

import hashlib
import hmac
import secrets
from typing import Any

from pydantic import BaseModel, ConfigDict

AUTH_HEADER = "Authorization"
NONCE_HEADER = "X-Meridian-Nonce"
PROOF_HEADER = "X-Meridian-Dispatcher-Proof"
TEMPLATE_HEADER = "X-Meridian-Template"
ACTOR_HEADER = "X-Meridian-Actor"
MIN_TOKEN_BYTES = 32


class RunAnswer(BaseModel):
    """What the dispatcher returns for one run. error is None when the actor answered;
    worker_lost marks a run whose worker went away (retried once by the runner)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: str
    status_code: int | None = None
    actor_response: str | None = None
    final: dict[str, Any] | None = None
    phases: list[tuple[str, int]] = []
    golden_snapshot: str | None = None
    foreign_snapshots: list[str] = []
    deleted: bool = False
    worker_lost: bool = False
    # Where a lost attempt ran (phase 4b step 3), from its resume.
    worker_pod: str | None = None
    node: str | None = None
    error: str | None = None


def new_token() -> str:
    return secrets.token_urlsafe(MIN_TOKEN_BYTES)


def new_nonce() -> str:
    return secrets.token_hex(16)


def proof(token: str, nonce: str, body: bytes) -> str:
    message = f"{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def proof_ok(token: str, nonce: str, body: bytes, given: str | None) -> bool:
    return given is not None and hmac.compare_digest(proof(token, nonce, body), given)


def bearer_ok(token: str, header: str | None) -> bool:
    if header is None or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header.removeprefix("Bearer ").encode(), token.encode())
