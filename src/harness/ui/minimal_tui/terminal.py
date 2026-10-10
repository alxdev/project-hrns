from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from harness.agent.loop import TextEvent, ToolCallEvent, ToolResultEvent
from harness.api.types import ToolDefinition


class TerminalUI:
    """The original minimal input/output terminal adapter."""

    async def emit(self, event: object) -> None:
        if isinstance(event, TextEvent):
            print(event.text)
        elif isinstance(event, ToolCallEvent):
            print(f"[tool] {event.name} {json.dumps(event.arguments, sort_keys=True)}")
        elif isinstance(event, ToolResultEvent):
            prefix = "[tool error]" if event.result.is_error else "[tool result]"
            print(f"{prefix} {event.result.content}")

    async def approve(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        answer = await asyncio.to_thread(
            input,
            f"Approve {tool.name} {json.dumps(arguments, sort_keys=True)}? [y/N] ",
        )
        return answer.strip().lower() in {"y", "yes"}

    async def read(self) -> str:
        return await asyncio.to_thread(input, "❯ ")

    def write(self, text: str) -> None:
        print(text)

    def error(self, text: str) -> None:
        print(f"[error] {text}", file=sys.stderr)

    class _Thinking:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
            return False

    def thinking(self) -> _Thinking:
        return self._Thinking()
