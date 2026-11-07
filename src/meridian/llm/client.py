"""ModelClient: one Messages API call in, one ModelResponse out.

AnthropicHttpClient posts to https://api.anthropic.com/v1/messages with httpx. It runs
inside an OpenShell sandbox with the meridian-anthropic provider attached: the
ANTHROPIC_API_KEY it reads there is a placeholder, and the policy proxy writes the real
key into the x-api-key header at the allowed endpoint. A value that looks like a real key
(it starts with "sk-ant-") is refused before any request, so this client never sends a
real key itself. The key is never logged, printed or put in an error message.

TLS: the proxy terminates TLS with the supervisor's CA, which the sandbox trusts through
SSL_CERT_FILE. The context is built from SSL_CERT_FILE when it is set and from certifi
otherwise; certificate and hostname verification are always on.

Only POST /v1/messages is ever called. One retry, on a transport error only (no retry on
an HTTP status); the request body sent is exactly the canonical JSON that was hashed.
"""

import hashlib
import json
import os
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import certifi
import httpx
from pydantic import BaseModel, ConfigDict, Field

from meridian.ledger.hashing import canonical_json

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
API_KEY_ENV = "ANTHROPIC_API_KEY"
REAL_KEY_PREFIX = "sk-ant-"
CA_FILE_ENV = "SSL_CERT_FILE"

ErrorKind = Literal["config", "transport", "http_status", "bad_response"]


class ModelClientError(Exception):
    def __init__(self, kind: ErrorKind, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class ModelRequest(BaseModel):
    """One Messages API request. body() is what is sent and hashed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    system: str = Field(min_length=1)
    user: str = Field(min_length=1)
    max_tokens: int = Field(ge=1)
    # None leaves the parameter out (some models refuse non-default sampling values).
    temperature: float | None = None
    disable_thinking: bool = False

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": [{"role": "user", "content": self.user}],
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.disable_thinking:
            body["thinking"] = {"type": "disabled"}
        return body

    def body_bytes(self) -> bytes:
        return canonical_json(self.body()).encode("utf-8")

    def sha256(self) -> str:
        return hashlib.sha256(self.body_bytes()).hexdigest()


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model: str
    stop_reason: str | None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def ssl_context(env: Mapping[str, str] | None = None) -> ssl.SSLContext:
    """Trust SSL_CERT_FILE when set (the OpenShell supervisor's bundle), else certifi.

    Verification is never turned off: the context always requires a valid certificate
    for the right host.
    """
    environ = os.environ if env is None else env
    cafile = environ.get(CA_FILE_ENV) or certifi.where()
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    return ctx


def parse_response(raw: bytes, latency_ms: int) -> ModelResponse:
    try:
        data = json.loads(raw)
        blocks = data["content"]
        text = "".join(b["text"] for b in blocks if b.get("type") == "text")
        usage = data.get("usage") or {}
        return ModelResponse(
            text=text,
            model=str(data.get("model", "")),
            stop_reason=data.get("stop_reason"),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            latency_ms=latency_ms,
            response_sha256=hashlib.sha256(raw).hexdigest(),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ModelClientError("bad_response", f"not a Messages API response: {exc}") from exc


class ModelClient(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


class AnthropicHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        url: str = MESSAGES_URL,
        env: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._url = url
        self._env = os.environ if env is None else env
        self._timer = timer
        self.ssl_context = ssl_context(self._env)
        self._http = httpx.Client(
            verify=self.ssl_context,
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,  # the CA comes from ssl_context; no proxy settings are used
        )

    def _headers(self) -> dict[str, str]:
        key = self._env.get(API_KEY_ENV)
        if not key:
            raise ModelClientError("config", f"{API_KEY_ENV} is not set")
        if key.startswith(REAL_KEY_PREFIX):
            # Never in the message: only that it looked like a real key.
            raise ModelClientError(
                "config",
                f"{API_KEY_ENV} holds a real key; this client only runs behind the "
                "OpenShell provider, which supplies a placeholder",
            )
        return {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def complete(self, request: ModelRequest) -> ModelResponse:
        headers = self._headers()
        body = request.body_bytes()
        start = self._timer()
        response: httpx.Response | None = None
        for attempt in (1, 2):
            try:
                response = self._http.post(self._url, content=body, headers=headers)
                break
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise ModelClientError(
                        "transport", f"{type(exc).__name__} after one retry: {exc}"
                    ) from exc
        assert response is not None
        latency_ms = round((self._timer() - start) * 1000)
        if response.status_code != 200:
            raise ModelClientError(
                "http_status",
                f"HTTP {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )
        return parse_response(response.content, latency_ms)

    def close(self) -> None:
        self._http.close()


@dataclass
class FakeModelClient:
    """Returns scripted texts (or raises scripted errors) in order; records each request."""

    script: Sequence[str | ModelClientError] = ()
    input_tokens: int = 100
    output_tokens: int = 40
    model: str = "fake-model"
    requests: list[ModelRequest] = field(default_factory=list)

    def complete(self, request: ModelRequest) -> ModelResponse:
        if len(self.requests) >= len(self.script):
            raise AssertionError("FakeModelClient ran out of scripted responses")
        item = self.script[len(self.requests)]
        self.requests.append(request)
        if isinstance(item, ModelClientError):
            raise item
        raw = json.dumps(
            {
                "model": self.model,
                "content": [{"type": "text", "text": item}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
            }
        ).encode("utf-8")
        return parse_response(raw, latency_ms=1)
