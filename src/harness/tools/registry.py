from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import Tool, ToolContext


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name].definition for name in sorted(self._tools)]

    async def execute(
        self,
        context: ToolContext,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(name, f"unknown tool: {name}", is_error=True)
        try:
            return await tool.execute(context, arguments)
        except Exception as exc:
            return ToolResult(name, f"tool failed: {exc}", is_error=True)
