"""Requested model selection and current Chat Completions compatibility, using mocked HTTP."""

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from harness.api.types import TextBlock, ToolCall, ToolDefinition, UserMessage
from harness.model_catalog import ModelCatalog, build_provider_for_choice
from harness.provider.base import ProviderResponseCompleted
from harness.provider.openai import OpenAIProvider

ROOT = Path(__file__).parents[1]
MODELS = ("gpt-4.1-mini", "gpt-5-mini", "gpt-5.6-luna")


@pytest.mark.parametrize("model", MODELS)
def test_requested_model_registered_selectable_and_uses_existing_credentials(monkeypatch, model):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    catalog = ModelCatalog.load(ROOT)
    choice = catalog.find(f"openai/{model}")
    assert choice.model == model and choice.provider == "openai"
    assert choice.options == {"apiKeyEnv": "OPENAI_API_KEY"}
    assert f"openai/{model}" in catalog.describe()
    assert catalog.preferred_key("openai", model) == choice.key
    assert next(status for status in catalog.statuses() if status.key == choice.key).available
    provider = build_provider_for_choice(choice)
    assert isinstance(provider, OpenAIProvider) and provider.model == model
    assert provider.max_tokens == 4096
    monkeypatch.delenv("OPENAI_API_KEY")
    status = next(status for status in catalog.statuses() if status.key == choice.key)
    assert not status.available and status.reason == "OPENAI_API_KEY is not set"


def test_existing_openai_default_and_first_picker_entry_unchanged(monkeypatch):
    monkeypatch.delenv("HARNESS_MODEL", raising=False)
    assert OpenAIProvider(api_key="test-only-key").model == "gpt-4o-mini"
    catalog = ModelCatalog.load(ROOT)
    assert catalog.preferred_key("openai", None) == "openai/gpt-4o-mini"


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("tool_call", [False, True])
async def test_requested_model_default_payload_and_responses(
    monkeypatch, model, streaming, tool_call
):
    tool = ToolDefinition("lookup", "Look up a value", {"type": "object"})
    seen = []

    def respond(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-only-key"
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload["model"] == model
        assert payload["max_completion_tokens"] == 4096
        assert "max_tokens" not in payload
        assert "temperature" not in payload
        if model == "gpt-5.6-luna":
            assert payload["reasoning_effort"] == "none"
        else:
            assert "reasoning_effort" not in payload
        assert payload["messages"] == [
            {"role": "system", "content": "Keep this system prompt"},
            {"role": "user", "content": "Look up α"},
        ]
        assert payload["tools"] == [OpenAIProvider._tool_to_wire(tool)]
        if streaming:
            assert payload["stream"] is True
            delta = (
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"value":"α"}'},
                        }
                    ]
                }
                if tool_call
                else {"content": "Found α"}
            )
            chunks = [
                {"choices": [{"delta": delta}]},
                {
                    "choices": [
                        {"delta": {}, "finish_reason": "tool_calls" if tool_call else "stop"}
                    ]
                },
            ]
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, text=body + "data: [DONE]\n\n"
            )
        assert "stream" not in payload
        message = (
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"value":"α"}'},
                    }
                ]
            }
            if tool_call
            else {"content": "Found α"}
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if tool_call else "stop"}
                ]
            },
        )

    client_type = httpx.AsyncClient
    factory = Mock(
        side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs)
    )
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    provider = OpenAIProvider(
        api_key="test-only-key", model=model, system="Keep this system prompt"
    )
    if streaming:
        events = [event async for event in provider.stream([UserMessage("Look up α")], [tool])]
        completed = [
            event.response for event in events if isinstance(event, ProviderResponseCompleted)
        ]
        assert len(completed) == 1
        response = completed[0]
    else:
        response = await provider.send([UserMessage("Look up α")], [tool])
    assert len(seen) == 1
    assert response.stop_reason == ("tool_use" if tool_call else "end_turn")
    assert response.content == (
        [ToolCall("call-1", "lookup", {"value": "α"})] if tool_call else [TextBlock("Found α")]
    )


@pytest.mark.parametrize("streaming", [False, True])
async def test_set_model_applies_luna_compatibility_only_with_tools(monkeypatch, streaming):
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload["max_completion_tokens"] == 4096
        assert payload["messages"][0]["content"] == "Stable system context"
        if streaming:
            return httpx.Response(
                200,
                text='data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    provider = OpenAIProvider(api_key="test-only-key", system="Stable system context")
    tool = ToolDefinition("lookup", "Look up a value", {"type": "object"})
    # Switch into Luna, away again, and to a similar but non-exact model ID.
    models = (*MODELS, "gpt-5-mini", "gpt-5.6-luna-other")
    for model in models:
        provider.set_model(model)
        for tools in ([], [tool]):
            if streaming:
                _ = [event async for event in provider.stream([], tools)]
            else:
                await provider.send([], tools)
            payload = seen[-1]
            assert payload["model"] == model
            if model == "gpt-5.6-luna" and tools:
                assert payload["reasoning_effort"] == "none"
            else:
                assert "reasoning_effort" not in payload
            assert ("tools" in payload) == bool(tools)
    assert len(seen) == len(models) * 2
