from __future__ import annotations

import json
import sys
from typing import Any

from harness.agent.loop import TextEvent, ToolCallEvent, ToolResultEvent
from harness.api.types import ToolDefinition


class TerminalUI:
    async def emit(self, event: object) -> None:
        if isinstance(event, TextEvent):
            print(event.text)
        elif isinstance(event, ToolCallEvent):
            print(f"[tool] {event.name} {json.dumps(event.arguments, sort_keys=True)}")
        elif isinstance(event, ToolResultEvent):
            prefix = "[tool error]" if event.result.is_error else "[tool result]"
            print(f"{prefix} {event.result.content}")

    async def approve(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        answer = await __import__("asyncio").to_thread(
            input,
            f"Approve {tool.name} {json.dumps(arguments, sort_keys=True)}? [y/N] ",
        )
        return answer.strip().lower() in {"y", "yes"}

    async def run(self, agent) -> None:
        while True:
            try:
                prompt = await __import__("asyncio").to_thread(input, "❯ ")
            except EOFError:
                print()
                return
            if prompt.strip().lower() in {"/exit", "/quit"}:
                return
            if not prompt.strip():
                continue
            try:
                await agent.send(prompt)
            except Exception as exc:
                print(f"[error] {exc}", file=sys.stderr)

