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


class AnthropicProvider:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        system: str = "You are a helpful coding assistant.",
        max_tokens: int = 4096,
        base_url: str = "https://api.anthropic.com",
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")
        self._model = model or os.environ.get("HARNESS_MODEL", "claude-sonnet-4-5")
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

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": [self._message_to_wire(message) for message in messages],
        }
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.base_url}/v1/messages",
                headers=headers,
                json=payload,
            )
        if response.is_error:
            raise RuntimeError(f"provider request failed ({response.status_code}): {response.text}")

        data = response.json()
        content = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                content.append(TextBlock(block.get("text", "")))
            elif block.get("type") == "tool_use":
                content.append(
                    ToolCall(
                        id=block["id"],
                        name=block["name"],
                        input=block.get("input", {}),
                    )
                )
        stop_reason = data.get("stop_reason", "unknown")
        if stop_reason not in {"end_turn", "tool_use", "max_tokens"}:
            stop_reason = "unknown"
        return ProviderResponse(content=content, stop_reason=stop_reason)

    @staticmethod
    def _message_to_wire(message: Message) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCall):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
            elif isinstance(block, ToolResult):
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
        return {"role": message.role, "content": blocks}
