"""LocalModelClient: one Ollama /api/chat call in, one LocalModelResponse out.

OllamaHttpClient posts to the Ollama HTTP API with httpx (no Ollama SDK). It runs inside
the safety.local_model sandbox, whose policy allows only GET /api/tags and POST /api/chat
on the one host address Ollama listens on (docs/local-model-notes.md). Requests are
deterministic: stream false, format json, temperature 0 and a fixed seed, num_ctx from
config, and think false so a thinking model answers without a reasoning trace.

Traceability: the request body sent is exactly the canonical JSON that was hashed, the
response sha256 is of the raw body received, and served_digest reads the model's digest
from /api/tags so a run can refuse a model that is not the pinned one.

Only message.content becomes the response text. A reasoning trace (message.thinking)
never reaches a validator: the response records only its length, and a validator rejects
a response that carried one.
"""

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from meridian.ledger.hashing import canonical_json

CHAT_PATH = "/api/chat"
TAGS_PATH = "/api/tags"

LocalErrorKind = Literal[
    "config", "transport", "http_status", "bad_response", "not_served", "digest_mismatch"
]


class LocalModelError(Exception):
    def __init__(self, kind: LocalErrorKind, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class LocalModelOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature: float = Field(0.0, ge=0.0)
    seed: int
    num_ctx: int = Field(ge=256)
    num_predict: int = Field(ge=1)


class LocalModelRequest(BaseModel):
    """One /api/chat request. body() is what is sent and hashed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    system: str = Field(min_length=1)
    user: str = Field(min_length=1)
    options: LocalModelOptions
    # Sent as "think": false. A model without a thinking mode accepts it (measured on
    # Ollama 0.40.15 with phi4-mini and granite4).
    think: bool = False

    def body(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": self.user},
            ],
            "stream": False,
            "format": "json",
            "think": self.think,
            "options": self.options.model_dump(mode="json"),
        }

    def body_bytes(self) -> bytes:
        return canonical_json(self.body()).encode("utf-8")

    def sha256(self) -> str:
        return hashlib.sha256(self.body_bytes()).hexdigest()


class LocalModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str  # message.content only
    model: str
    done_reason: str | None
    input_tokens: int = Field(ge=0)  # prompt_eval_count
    output_tokens: int = Field(ge=0)  # eval_count
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Length of message.thinking, if the model returned one. Its text is never kept.
    thinking_chars: int = Field(0, ge=0)


def parse_chat_response(raw: bytes, latency_ms: int) -> LocalModelResponse:
    try:
        data = json.loads(raw)
        message = data["message"]
        content = message["content"]
        if not isinstance(content, str):
            raise TypeError("message.content is not a string")
        thinking = message.get("thinking") or ""
        return LocalModelResponse(
            text=content,
            model=str(data.get("model", "")),
            done_reason=data.get("done_reason"),
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            latency_ms=latency_ms,
            response_sha256=hashlib.sha256(raw).hexdigest(),
            thinking_chars=len(thinking) if isinstance(thinking, str) else 0,
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LocalModelError("bad_response", f"not an /api/chat response: {exc}") from exc


def parse_tags(raw: bytes) -> dict[str, str]:
    """Model name to digest, from an /api/tags body."""
    try:
        data = json.loads(raw)
        return {str(m["name"]): str(m["digest"]) for m in data["models"]}
    except (ValueError, KeyError, TypeError) as exc:
        raise LocalModelError("bad_response", f"not an /api/tags response: {exc}") from exc


class LocalModelClient(Protocol):
    def chat(self, request: LocalModelRequest) -> LocalModelResponse: ...

    def served_digest(self, model: str) -> str: ...


def require_pinned(model: str, served: str, pinned: str) -> None:
    """LocalModelError("digest_mismatch") unless the served digest is the pinned one."""
    if served != pinned:
        raise LocalModelError(
            "digest_mismatch", f"{model} is served as {served}, but {pinned} is pinned"
        )


def check_digest(client: LocalModelClient, model: str, pinned: str) -> str:
    """The served digest, if it is the pinned one."""
    served = client.served_digest(model)
    require_pinned(model, served, pinned)
    return served


class OllamaHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._timer = timer
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,  # no proxy settings: the policy proxy is transparent
        )

    def _send(self, method: str, path: str, body: bytes | None) -> httpx.Response:
        headers = {"content-type": "application/json"} if body is not None else {}
        for attempt in (1, 2):
            try:
                return self._http.request(method, path, content=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise LocalModelError(
                        "transport", f"{type(exc).__name__} after one retry: {exc}"
                    ) from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _ok(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise LocalModelError(
                "http_status",
                f"HTTP {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )

    def chat(self, request: LocalModelRequest) -> LocalModelResponse:
        start = self._timer()
        response = self._send("POST", CHAT_PATH, request.body_bytes())
        latency_ms = round((self._timer() - start) * 1000)
        self._ok(response)
        return parse_chat_response(response.content, latency_ms)

    def served_digest(self, model: str) -> str:
        response = self._send("GET", TAGS_PATH, None)
        self._ok(response)
        tags = parse_tags(response.content)
        if model not in tags:
            raise LocalModelError("not_served", f"{model} is not served by this Ollama")
        return tags[model]

    def close(self) -> None:
        self._http.close()


@dataclass
class FakeLocalModelClient:
    """Scripted contents (or errors) in order; records each request. For tests and CI."""

    script: Sequence[str | LocalModelError] = ()
    digests: Mapping[str, str] = field(default_factory=dict)
    input_tokens: int = 120
    output_tokens: int = 30
    thinking: str = ""
    done_reason: str = "stop"
    requests: list[LocalModelRequest] = field(default_factory=list)

    def chat(self, request: LocalModelRequest) -> LocalModelResponse:
        if len(self.requests) >= len(self.script):
            raise AssertionError("FakeLocalModelClient ran out of scripted responses")
        item = self.script[len(self.requests)]
        self.requests.append(request)
        if isinstance(item, LocalModelError):
            raise item
        message: dict[str, str] = {"role": "assistant", "content": item}
        if self.thinking:
            message["thinking"] = self.thinking
        raw = json.dumps(
            {
                "model": request.model,
                "message": message,
                "done": True,
                "done_reason": self.done_reason,
                "prompt_eval_count": self.input_tokens,
                "eval_count": self.output_tokens,
            }
        ).encode("utf-8")
        return parse_chat_response(raw, latency_ms=1)

    def served_digest(self, model: str) -> str:
        if model not in self.digests:
            raise LocalModelError("not_served", f"{model} is not served by this Ollama")
        return self.digests[model]
