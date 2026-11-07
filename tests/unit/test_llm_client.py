"""AnthropicHttpClient against an httpx MockTransport, and the fake."""

import hashlib
import json
import ssl
from pathlib import Path

import certifi
import httpx
import pytest

from meridian.llm.client import (
    ANTHROPIC_VERSION,
    MESSAGES_URL,
    AnthropicHttpClient,
    FakeModelClient,
    ModelClientError,
    ModelRequest,
    ssl_context,
)

PLACEHOLDER = "openshell-placeholder-value"
ENV = {"ANTHROPIC_API_KEY": PLACEHOLDER}
REQUEST = ModelRequest(
    model="claude-haiku-5-5",
    system="sys",
    user="hello",
    max_tokens=300,
    temperature=0.0,
    disable_thinking=True,
)
OK_BODY = {
    "model": "claude-haiku-5-5",
    "content": [{"type": "text", "text": '{"headline": "h", "explanation": "e"}'}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 120, "output_tokens": 30},
}


def _client(handler: httpx.MockTransport, env: dict[str, str] | None = None) -> AnthropicHttpClient:
    return AnthropicHttpClient(timeout_seconds=5.0, env=env or ENV, transport=handler)


def test_posts_the_canonical_body_with_the_required_headers() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    response = _client(httpx.MockTransport(handle)).complete(REQUEST)
    (sent,) = seen
    assert sent.method == "POST"
    assert str(sent.url) == MESSAGES_URL
    assert sent.headers["x-api-key"] == PLACEHOLDER
    assert sent.headers["anthropic-version"] == ANTHROPIC_VERSION
    assert sent.content == REQUEST.body_bytes()
    assert hashlib.sha256(sent.content).hexdigest() == REQUEST.sha256()
    body = json.loads(sent.content)
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 300
    assert body["thinking"] == {"type": "disabled"}
    assert response.text == '{"headline": "h", "explanation": "e"}'
    assert (response.input_tokens, response.output_tokens) == (120, 30)


def test_temperature_none_is_left_out() -> None:
    body = REQUEST.model_copy(update={"temperature": None, "disable_thinking": False}).body()
    assert "temperature" not in body
    assert "thinking" not in body


def test_one_retry_on_a_transport_error() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(200, json=OK_BODY)

    assert _client(httpx.MockTransport(handle)).complete(REQUEST).output_tokens == 30
    assert calls == 2


def test_a_second_transport_error_is_raised() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ModelClientError) as info:
        _client(httpx.MockTransport(handle)).complete(REQUEST)
    assert info.value.kind == "transport"
    assert calls == 2


def test_an_http_error_is_not_retried() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(529, json={"type": "error"})

    with pytest.raises(ModelClientError) as info:
        _client(httpx.MockTransport(handle)).complete(REQUEST)
    assert (info.value.kind, info.value.status, calls) == ("http_status", 529, 1)


def test_a_real_looking_key_is_refused_and_never_echoed() -> None:
    secret = "sk-ant-" + "x" * 20

    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    with pytest.raises(ModelClientError) as info:
        _client(httpx.MockTransport(handle), {"ANTHROPIC_API_KEY": secret}).complete(REQUEST)
    assert info.value.kind == "config"
    assert secret not in str(info.value)


def test_a_missing_key_is_a_config_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    with pytest.raises(ModelClientError) as info:
        AnthropicHttpClient(
            timeout_seconds=5.0, env={}, transport=httpx.MockTransport(handle)
        ).complete(REQUEST)
    assert info.value.kind == "config"


def test_a_body_that_is_not_a_messages_response_is_bad_response() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    with pytest.raises(ModelClientError) as info:
        _client(httpx.MockTransport(handle)).complete(REQUEST)
    assert info.value.kind == "bad_response"


@pytest.mark.parametrize("use_env_ca", [True, False])
def test_tls_verification_is_always_on(use_env_ca: bool) -> None:
    env = {"SSL_CERT_FILE": certifi.where()} if use_env_ca else {}
    ctx = ssl_context(env)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    client = AnthropicHttpClient(timeout_seconds=5.0, env={**ENV, **env})
    assert client.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert client.ssl_context.check_hostname is True
    client.close()


def test_the_ca_comes_from_ssl_cert_file_when_set(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-bundle.crt"
    with pytest.raises(FileNotFoundError):
        ssl_context({"SSL_CERT_FILE": str(missing)})  # the variable is really used


def test_fake_client_scripts_texts_and_errors() -> None:
    fake = FakeModelClient(script=["one", ModelClientError("transport", "down")])
    assert fake.complete(REQUEST).text == "one"
    with pytest.raises(ModelClientError):
        fake.complete(REQUEST)
    assert fake.requests == [REQUEST, REQUEST]
