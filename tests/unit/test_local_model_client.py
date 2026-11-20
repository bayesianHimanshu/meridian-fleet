"""OllamaHttpClient against an httpx MockTransport, the fake, the job and digest pinning."""

import hashlib
import json

import httpx
import pytest
from pydantic import ValidationError

from meridian.llm.local import (
    CHAT_PATH,
    TAGS_PATH,
    FakeLocalModelClient,
    LocalModelError,
    LocalModelOptions,
    LocalModelRequest,
    OllamaHttpClient,
    check_digest,
)
from meridian.llm.local_job import LocalModelJob, run_local_job
from meridian.platform.config import LocalModelSettings

BASE = "http://172.17.0.1:11434"
MODEL = "granite4:3b"
DIGEST = "a" * 64
OTHER = "b" * 64
REQUEST = LocalModelRequest(
    model=MODEL,
    system="sys",
    user="Term A: Hypotension. Term B: Hypertension.",
    options=LocalModelOptions(temperature=0.0, seed=7, num_ctx=4096, num_predict=128),
)
CHAT_BODY = {
    "model": MODEL,
    "message": {"role": "assistant", "content": '{"same_event": "no", "reason": "r."}'},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 54,
    "eval_count": 21,
}
TAGS_BODY = {"models": [{"name": MODEL, "model": MODEL, "digest": DIGEST}]}


def _client(handle: httpx.MockTransport) -> OllamaHttpClient:
    return OllamaHttpClient(base_url=BASE, timeout_seconds=5.0, transport=handle)


def _ollama(seen: list[httpx.Request], chat: object = CHAT_BODY) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == TAGS_PATH:
            return httpx.Response(200, json=TAGS_BODY)
        return httpx.Response(200, content=json.dumps(chat).encode())

    return httpx.MockTransport(handle)


def test_the_request_is_deterministic_json_without_streaming_or_thinking() -> None:
    body = REQUEST.body()
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["think"] is False
    assert body["options"] == {"temperature": 0.0, "seed": 7, "num_ctx": 4096, "num_predict": 128}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert REQUEST.sha256() == hashlib.sha256(REQUEST.body_bytes()).hexdigest()


def test_posts_exactly_the_hashed_body_to_api_chat() -> None:
    seen: list[httpx.Request] = []
    response = _client(_ollama(seen)).chat(REQUEST)
    (sent,) = seen
    assert sent.method == "POST"
    assert str(sent.url) == BASE + CHAT_PATH
    assert sent.content == REQUEST.body_bytes()
    assert hashlib.sha256(sent.content).hexdigest() == REQUEST.sha256()
    assert response.text == CHAT_BODY["message"]["content"]  # type: ignore[index]
    assert (response.input_tokens, response.output_tokens) == (54, 21)
    assert response.done_reason == "stop"
    raw = json.dumps(CHAT_BODY).encode()
    assert response.response_sha256 == hashlib.sha256(raw).hexdigest()


def test_a_reasoning_trace_never_becomes_response_text() -> None:
    thinking = "Thinking Process: the user wants to know whether two terms..."
    body = {**CHAT_BODY, "message": {**CHAT_BODY["message"], "thinking": thinking}}  # type: ignore[dict-item]
    response = _client(_ollama([], chat=body)).chat(REQUEST)
    assert response.text == CHAT_BODY["message"]["content"]  # type: ignore[index]
    assert "Thinking" not in response.model_dump_json()
    assert response.thinking_chars == len(thinking)


def test_served_digest_reads_api_tags() -> None:
    seen: list[httpx.Request] = []
    assert _client(_ollama(seen)).served_digest(MODEL) == DIGEST
    assert [(r.method, r.url.path) for r in seen] == [("GET", TAGS_PATH)]
    with pytest.raises(LocalModelError) as exc:
        _client(_ollama([])).served_digest("qwen3.5:4b-q4_K_M")
    assert exc.value.kind == "not_served"


def test_an_http_error_is_a_typed_error_and_a_transport_error_is_retried_once() -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "policy_denied"})

    with pytest.raises(LocalModelError) as exc:
        _client(httpx.MockTransport(denied)).chat(REQUEST)
    assert (exc.value.kind, exc.value.status) == ("http_status", 403)

    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=CHAT_BODY)

    assert _client(httpx.MockTransport(flaky)).chat(REQUEST).text
    assert len(calls) == 2


def test_check_digest_refuses_a_model_that_is_not_the_pinned_one() -> None:
    fake = FakeLocalModelClient(digests={MODEL: DIGEST})
    assert check_digest(fake, MODEL, DIGEST) == DIGEST
    with pytest.raises(LocalModelError) as exc:
        check_digest(fake, MODEL, OTHER)
    assert exc.value.kind == "digest_mismatch"


def test_a_job_with_a_different_served_digest_sends_nothing() -> None:
    fake = FakeLocalModelClient(
        script=['{"same_event": "no", "reason": "r."}'], digests={MODEL: OTHER}
    )
    job = LocalModelJob(model=MODEL, pinned_digest=DIGEST, requests=(REQUEST,))
    results = run_local_job(job, fake)
    assert fake.requests == []
    assert results.served_digest == OTHER
    (only,) = results.results
    assert only.response is None
    assert only.error_kind == "digest_mismatch"
    assert only.request_sha256 == REQUEST.sha256()


def test_a_job_with_the_pinned_digest_sends_every_request_in_order() -> None:
    other = REQUEST.model_copy(update={"user": "Term A: Rash. Term B: Skin rash."})
    fake = FakeLocalModelClient(
        script=['{"a": 1}', LocalModelError("transport", "down")], digests={MODEL: DIGEST}
    )
    results = run_local_job(
        LocalModelJob(model=MODEL, pinned_digest=DIGEST, requests=(REQUEST, other)), fake
    )
    assert results.served_digest == DIGEST
    assert [r.request_sha256 for r in results.results] == [REQUEST.sha256(), other.sha256()]
    assert results.results[0].response is not None
    assert results.results[1].error_kind == "transport"


def test_a_job_names_one_model() -> None:
    with pytest.raises(ValidationError):
        LocalModelJob(model="phi4-mini:3.8b-q4_K_M", pinned_digest=DIGEST, requests=(REQUEST,))


def test_settings_refuse_a_model_without_a_pinned_digest() -> None:
    two = ("x:1b", "y:1b")
    pinned = {MODEL: DIGEST, "x:1b": DIGEST, "y:1b": DIGEST}
    with pytest.raises(ValidationError, match="no pinned digest"):
        LocalModelSettings(model=MODEL, adjudicator_models=two, digests={})
    with pytest.raises(ValidationError, match="y:1b has no pinned digest"):
        LocalModelSettings(
            model=MODEL, adjudicator_models=two, digests={MODEL: DIGEST, "x:1b": DIGEST}
        )
    with pytest.raises(ValidationError, match="64 hex"):
        LocalModelSettings(
            model=MODEL, adjudicator_models=two, digests={**pinned, MODEL: "sha256:abc"}
        )
    with pytest.raises(ValidationError, match="two or more different"):
        LocalModelSettings(model=MODEL, adjudicator_models=("x:1b", "x:1b"), digests=pinned)
    settings = LocalModelSettings(model=MODEL, adjudicator_models=two, digests=pinned)
    assert settings.pinned() == settings.pinned("y:1b") == DIGEST
