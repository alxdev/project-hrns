import importlib
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from harness.api.types import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolDefinition,
    ToolResult,
    UserMessage,
)
from harness.config import parse_config
from harness.provider import http_transport
from harness.provider.anthropic import AnthropicProvider
from harness.provider.local_models.local_openai import LocalOpenAICompatibleProvider
from harness.provider.openai import OpenAIProvider


@pytest.fixture(autouse=True)
def reset_logging():
    http_transport.configure_prompt_logging(False)
    yield
    http_transport.configure_prompt_logging(False)


@pytest.fixture(params=["OpenAI", "Anthropic", "local OpenAI-compatible"])
def adapter(request, tmp_path):
    if request.param == "OpenAI":
        provider = OpenAIProvider(api_key="secret-auth", model="test-model")
    elif request.param == "Anthropic":
        provider = AnthropicProvider(api_key="secret-auth", model="test-model")
    else:
        path = tmp_path / "local_models.json"
        path.write_text(
            json.dumps(
                {
                    "provider": {
                        "server": {
                            "options": {
                                "baseURL": "https://models.example",
                                "apiKey": "secret-auth",
                            },
                            "models": {"test-model": {}},
                        }
                    },
                    "model": "server/test-model",
                }
            )
        )
        provider = LocalOpenAICompatibleProvider(config_path=path)
    return request.param, provider


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("status", [200, 400])
async def test_actual_wire_prompt_logging(adapter, monkeypatch, capsys, stream, enabled, status):
    label, provider = adapter
    provider.system = "Exact system\n你好 🔎"
    messages = [
        UserMessage("original request\nwith Unicode: café"),
        AssistantMessage([ToolCall("call-1", "inspect", {"path": "a.txt"})]),
        Message("user", [ToolResult("call-1", "prior result in actual context")]),
    ]
    tools = [ToolDefinition("inspect", "Inspect a file", {"type": "object", "properties": {}})]
    outgoing = []
    client_type = httpx.AsyncClient

    def respond(request):
        outgoing.append(json.loads(request.content))
        # A real outgoing request is logged even if the server rejects it.
        if status == 400:
            return httpx.Response(400, text="not a prompt: upstream failure")
        if stream:
            chunk = (
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
                if label == "Anthropic"
                else {"choices": [{"delta": {}, "finish_reason": "stop"}]}
            )
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                "content": [{"type": "text", "text": "answer"}],
                "stop_reason": "end_turn",
            },
        )

    factory = Mock(
        side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs)
    )
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    spy = AsyncMock(wraps=http_transport._log_prompt)
    monkeypatch.setattr(http_transport, "_log_prompt", spy)
    http_transport.configure_prompt_logging(enabled)

    async def invoke():
        if stream:
            return [event async for event in provider.stream(messages, tools)]
        return await provider.send(messages, tools)

    if status == 400:
        with pytest.raises(RuntimeError, match="provider request failed"):
            await invoke()
    else:
        await invoke()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(outgoing) == 1
    if not enabled:
        assert captured.err == ""
        spy.assert_not_called()
        return
    spy.assert_awaited_once()
    expected = {
        key: value
        for key, value in outgoing[0].items()
        if key in {"model", "system", "messages", "tools"}
    }
    assert json.loads(captured.err) == {"provider": label, **expected}
    assert "Exact system" in captured.err
    assert "你好 🔎" in captured.err
    assert "Inspect a file" in captured.err
    assert "prior result in actual context" in captured.err
    for excluded in (
        "secret-auth",
        "Authorization",
        "x-api-key",
        "max_tokens",
        "max_completion_tokens",
        "upstream failure",
        "https://",
    ):
        assert excluded not in captured.err


async def test_logging_covers_new_providers_and_changed_context(monkeypatch, capsys):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda request: httpx.Response(400)), **kwargs
        ),
    )
    http_transport.configure_prompt_logging(True)
    root = OpenAIProvider(api_key="key", model="root-model", system="root system")
    child = AnthropicProvider(api_key="key", model="child-model", system="child system")
    switched = OpenAIProvider(api_key="key", model="switched-model", system=root.system)
    for provider, text in (
        (root, "root request"),
        (child, "child assignment"),
        (switched, "after model switch"),
        (root, "compaction summary request"),
    ):
        with pytest.raises(RuntimeError):
            await provider.send([UserMessage(text)], [])
    logs = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [log["model"] for log in logs] == [
        "root-model",
        "child-model",
        "switched-model",
        "root-model",
    ]
    assert "child system" in json.dumps(logs[1])
    assert "root system" not in json.dumps(logs[1])
    assert "compaction summary request" in json.dumps(logs[3])
    http_transport.configure_prompt_logging(False)
    with pytest.raises(RuntimeError):
        await root.send([UserMessage("must not log")], [])
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", " TRUE ", "Yes"])
def test_log_prompt_sent_to_model_true_config(monkeypatch, value):
    monkeypatch.setenv("log_prompt_sent_to_model", value)
    assert parse_config([]).log_prompt_sent_to_model is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", " FALSE ", "No"])
def test_log_prompt_sent_to_model_false_config(monkeypatch, value):
    monkeypatch.setenv("log_prompt_sent_to_model", value)
    assert parse_config([]).log_prompt_sent_to_model is False


def test_log_prompt_sent_to_model_defaults_false_and_requires_exact_spelling(monkeypatch):
    monkeypatch.delenv("log_prompt_sent_to_model", raising=False)
    monkeypatch.setenv("LOG_PROMPT_SENT_TO_MODEL", "true")
    monkeypatch.setenv("debugg_log", "true")
    assert parse_config([]).log_prompt_sent_to_model is False


def test_log_prompt_sent_to_model_invalid_value_is_configuration_error(monkeypatch, capsys):
    monkeypatch.setenv("log_prompt_sent_to_model", "perhaps")
    with pytest.raises(SystemExit) as caught:
        parse_config([])
    assert caught.value.code == 2
    assert "log_prompt_sent_to_model must be true/false" in capsys.readouterr().err


async def test_main_configures_logging_after_dotenv_before_provider_setup(monkeypatch):
    main = importlib.import_module("harness.main")
    monkeypatch.delenv("log_prompt_sent_to_model", raising=False)
    monkeypatch.setattr(
        main, "load_dotenv", lambda: monkeypatch.setenv("log_prompt_sent_to_model", "true")
    )
    configure = Mock()
    monkeypatch.setattr(main, "configure_prompt_logging", configure)

    def stop_before_provider(*args):
        configure.assert_called_once_with(True)
        raise RuntimeError("setup boundary reached")

    monkeypatch.setattr(main, "load_project_context", stop_before_provider)
    with pytest.raises(RuntimeError, match="setup boundary reached"):
        await main.main([])
