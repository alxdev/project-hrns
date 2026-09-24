from __future__ import annotations

import os
from typing import Any, Sequence

from harness.api.types import (
    Message,
    ProviderResponse,
    TextBlock,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


class OpenAIProvider:
    """Provider adapter for OpenAI and OpenAI-compatible chat APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        system: str = "You are a helpful coding assistant.",
        max_tokens: int = 4096,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self._api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self._model = model or os.environ.get("HARNESS_MODEL", "gpt-4o-mini")
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
        import httpx

        wire_messages: list[dict[str, Any]] = [{"role": "system", "content": self.system}]
        for message in messages:
            translated = self._message_to_wire(message)
            if isinstance(translated, list):
                wire_messages.extend(translated)
            else:
                wire_messages.append(translated)

        payload: dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": self.max_tokens,
            "messages": wire_messages,
        }
        if tools:
            payload["tools"] = [self._tool_to_wire(tool) for tool in tools]

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
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
            raw_arguments = function.get("arguments", "{}")
            try:
                import json

                arguments = json.loads(raw_arguments)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"invalid tool arguments for {function.get('name')}: {exc}") from exc
            if not isinstance(arguments, dict):
                raise RuntimeError(f"tool arguments for {function.get('name')} must be an object")
            content.append(
                ToolCall(
                    id=tool_call["id"],
                    name=function["name"],
                    input=arguments,
                )
            )

        finish_reason = choices[0].get("finish_reason")
        stop_reason = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "length": "max_tokens",
        }.get(finish_reason, "unknown")
        return ProviderResponse(content=content, stop_reason=stop_reason)

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
                {
                    "role": "tool",
                    "tool_call_id": block.tool_use_id,
                    "content": block.content,
                }
                for block in tool_results
            ]

        text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
        tool_calls = [block for block in message.content if isinstance(block, ToolCall)]
        if message.role == "assistant":
            wire: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                import json

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
