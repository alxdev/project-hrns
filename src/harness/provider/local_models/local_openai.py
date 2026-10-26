from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

from harness.api.types import (
    Message,
    ProviderResponse,
    StopReason,
    TextBlock,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from harness.provider.base import ProviderResponseCompleted, ProviderStreamEvent, ProviderTextDelta
from harness.provider.http_transport import provider_http_client
from harness.provider.local_models.local_models import load_local_models, resolve_local_model


class LocalOpenAICompatibleProvider:
    """Local model adapter for APIs exposing OpenAI chat completions."""

    def __init__(
        self,
        model: str | None = None,
        config_path: Path | None = None,
        system: str = "You are a helpful coding assistant.",
        max_tokens: int = 4096,
    ) -> None:
        path = config_path or Path.cwd() / "local_models.json"
        provider_config, model_id = resolve_local_model(load_local_models(path), model, path)
        options = provider_config.options
        base_url = options.get("baseURL", options.get("base_url"))
        if not isinstance(base_url, str) or not base_url:
            raise ValueError(f"local provider {provider_config.name!r} requires options.baseURL")
        api_key = options.get("apiKey", options.get("api_key"))
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError(
                f"local provider {provider_config.name!r} options.apiKey must be a string"
            )
        self._api_key = api_key
        self._model = model_id
        self.system = system
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")

    @property
    def model(self) -> str:
        return self._model

    def set_model(self, model: str) -> None:
        self._model = model

    async def send(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> ProviderResponse:
        async with provider_http_client(
            provider="local OpenAI-compatible", base_url=self.base_url, operation="request"
        ) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(messages, tools),
            )
        if response.is_error:
            raise RuntimeError(f"provider request failed ({response.status_code}): {response.text}")
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("provider response contained no choices")
        message = choices[0].get("message", {})
        content: list[TextBlock | ToolCall] = []
        if message.get("content"):
            content.append(TextBlock(message["content"]))
        for tool_call in message.get("tool_calls", []) or []:
            function = tool_call.get("function", {})
            try:
                arguments = json.loads(function.get("arguments", "{}"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"invalid tool arguments for {function.get('name')}: {exc}"
                ) from exc
            if not isinstance(arguments, dict):
                raise RuntimeError(f"tool arguments for {function.get('name')} must be an object")
            content.append(ToolCall(tool_call["id"], function["name"], arguments))
        stop_reason = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "length": "max_tokens",
        }.get(choices[0].get("finish_reason") or "", "unknown")
        return ProviderResponse(content, cast("StopReason", stop_reason))

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderStreamEvent]:
        payload = self._payload(messages, tools)
        payload["stream"] = True
        text_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        async with provider_http_client(
            provider="local OpenAI-compatible",
            base_url=self.base_url,
            operation="streaming request",
        ) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise RuntimeError(
                        f"provider request failed ({response.status_code}): {response.text}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    chunk = json.loads(raw)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    text = delta.get("content")
                    if text:
                        text_parts.append(text)
                        yield ProviderTextDelta(text)
                    for item in delta.get("tool_calls", []) or []:
                        index = item.get("index", 0)
                        part = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        part["id"] += item.get("id", "")
                        function = item.get("function", {})
                        part["name"] += function.get("name", "")
                        part["arguments"] += function.get("arguments", "")
                    finish_reason = choice.get("finish_reason") or finish_reason
        blocks: list[TextBlock | ToolCall] = []
        if text_parts:
            blocks.append(TextBlock("".join(text_parts)))
        for part in (tool_parts[index] for index in sorted(tool_parts)):
            try:
                arguments = json.loads(part["arguments"] or "{}")
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"invalid tool arguments for {part['name']}: {exc}") from exc
            if not isinstance(arguments, dict):
                raise RuntimeError(f"tool arguments for {part['name']} must be an object")
            blocks.append(ToolCall(part["id"], part["name"], arguments))
        stop_reason = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "length": "max_tokens",
        }.get(finish_reason or "", "unknown")
        yield ProviderResponseCompleted(ProviderResponse(blocks, cast("StopReason", stop_reason)))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _payload(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> dict[str, Any]:
        wire_messages: list[dict[str, Any]] = [{"role": "system", "content": self.system}]
        for message in messages:
            translated = self._message_to_wire(message)
            wire_messages.extend(translated if isinstance(translated, list) else [translated])
        payload: dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": self.max_tokens,
            "messages": wire_messages,
        }
        if tools:
            payload["tools"] = [self._tool_to_wire(tool) for tool in tools]
        return payload

    @staticmethod
    def _tool_to_wire(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    @staticmethod
    def _message_to_wire(message: Message) -> dict[str, Any] | list[dict[str, Any]]:
        tool_results = [block for block in message.content if isinstance(block, ToolResult)]
        if message.role == "user" and tool_results and len(tool_results) == len(message.content):
            return [
                {"role": "tool", "tool_call_id": block.tool_use_id, "content": block.content}
                for block in tool_results
            ]
        text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
        tool_calls = [block for block in message.content if isinstance(block, ToolCall)]
        if message.role == "assistant":
            wire: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                wire["tool_calls"] = [
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input, separators=(",", ":")),
                        },
                    }
                    for block in tool_calls
                ]
            return wire
        return {"role": "user", "content": text}
