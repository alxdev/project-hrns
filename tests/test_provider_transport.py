import json
import socket
from collections.abc import AsyncIterator
from unittest.mock import Mock

import httpx
import pytest

from harness.provider.anthropic import AnthropicProvider
from harness.provider.base import ProviderResponseCompleted, ProviderTextDelta
from harness.provider.local_models.local_openai import LocalOpenAICompatibleProvider
from harness.provider.openai import OpenAIProvider

ENDPOINT = (
    "https://private-user:private-password@models.example:9443/private-path?token=private-query"
)
RAW_ERROR = f"request to {ENDPOINT} failed; Authorization: Bearer private-header"


@pytest.fixture(params=["OpenAI", "Anthropic", "local OpenAI-compatible"])
def provider(request, tmp_path):
    if request.param == "OpenAI":
        adapter = OpenAIProvider(api_key="private-header", base_url=ENDPOINT)
    elif request.param == "Anthropic":
        adapter = AnthropicProvider(api_key="private-header", base_url=ENDPOINT)
    else:
        config = tmp_path / "local_models.json"
        config.write_text(
            json.dumps(
                {
                    "provider": {
                        "server": {
                            "options": {"baseURL": ENDPOINT, "apiKey": "private-header"},
                            "models": {"test-model": {}},
                        }
                    },
                    "model": "server/test-model",
                }
            )
        )
        adapter = LocalOpenAICompatibleProvider(config_path=config)
    return request.param, adapter


def install_transport(monkeypatch, handler):
    client_type = httpx.AsyncClient
    factory = Mock(
        side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return factory


async def invoke(adapter, operation):
    if operation == "request":
        return await adapter.send([], [])
    return [event async for event in adapter.stream([], [])]


class FailingBody(httpx.AsyncByteStream):
    def __init__(self, error):
        self.error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b": keepalive\n\n"
        raise self.error


@pytest.mark.parametrize("operation", ["request", "streaming request"])
@pytest.mark.parametrize("stage", ["connect", "body"])
@pytest.mark.parametrize(
    ("failure", "category"),
    [
        ("dns", "DNS resolution failed"),
        ("dns-context", "DNS resolution failed"),
        (httpx.ConnectError, "connection failed"),
        (httpx.ConnectTimeout, "connection timed out"),
        (httpx.ReadTimeout, "response read timed out"),
        (httpx.WriteTimeout, "request write timed out"),
        (httpx.PoolTimeout, "connection pool timed out"),
        (httpx.TimeoutException, "transport timed out"),
        (httpx.ReadError, "response read failed"),
        (httpx.WriteError, "request write failed"),
        (httpx.RemoteProtocolError, "HTTP protocol error"),
        (httpx.ProxyError, "proxy connection failed"),
        (httpx.TransportError, "transport failure"),
    ],
)
async def test_transport_errors_are_contextual_and_sanitized(
    provider, monkeypatch, operation, stage, failure, category
):
    label, adapter = provider
    if failure in ("dns", "dns-context"):
        error = httpx.ConnectError(RAW_ERROR)
        intermediate = OSError(RAW_ERROR)
        intermediate.__cause__ = socket.gaierror(socket.EAI_NONAME, RAW_ERROR)
        if failure == "dns":
            error.__cause__ = intermediate
        else:
            error.__context__ = intermediate
    else:
        # DNS-looking text alone must never classify a connection failure as DNS.
        error = failure(f"Name resolution failed: {RAW_ERROR}")

    def handler(request):
        error.request = request
        if stage == "connect":
            raise error
        return httpx.Response(200, stream=FailingBody(error))

    factory = install_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError) as caught:
        await invoke(adapter, operation)

    assert str(caught.value) == (
        f"provider {operation} failed ({label}; host=models.example): {category}"
    )
    assert caught.value.__cause__ is error
    for secret in (
        "private-user",
        "private-password",
        "private-path",
        "private-query",
        "private-header",
        "9443",
        "token=",
        "Authorization",
        "https://",
    ):
        assert secret not in str(caught.value)
    factory.assert_called_once_with(timeout=120)


@pytest.mark.parametrize("operation", ["request", "streaming request"])
@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_http_status_errors_keep_existing_behavior(provider, monkeypatch, operation, status):
    _, adapter = provider
    install_transport(monkeypatch, lambda request: httpx.Response(status, text="upstream detail"))

    with pytest.raises(RuntimeError) as caught:
        await invoke(adapter, operation)

    assert str(caught.value) == f"provider request failed ({status}): upstream detail"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("operation", ["request", "streaming request"])
async def test_successful_responses_are_unchanged(provider, monkeypatch, operation):
    label, adapter = provider
    if operation == "request":
        response = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
            },
        )
    else:
        chunks = (
            [
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hello"},
                },
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            ]
            if label == "Anthropic"
            else [{"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}]
        )
        response = httpx.Response(
            200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        )
    install_transport(monkeypatch, lambda request: response)

    result = await invoke(adapter, operation)

    if operation == "streaming request":
        assert isinstance(result[0], ProviderTextDelta)
        assert result[0].text == "hello"
        assert isinstance(result[-1], ProviderResponseCompleted)
        result = result[-1].response
    assert result.content[0].text == "hello"
    assert result.stop_reason == "end_turn"


async def test_cyclic_exception_chain_does_not_hang(monkeypatch):
    adapter = OpenAIProvider(api_key="key", base_url=ENDPOINT)
    error = httpx.ConnectError(RAW_ERROR)
    error.__cause__ = error

    def handler(request):
        raise error

    install_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="connection failed"):
        await adapter.send([], [])
