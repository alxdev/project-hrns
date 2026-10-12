from __future__ import annotations

from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import ToolContext

from .manager import SubagentManager


class DelegationTool:
    """Tool exposed to an agent for delegating work to a named subagent."""

    def __init__(self, profiles: list[str]) -> None:
        self._manager: SubagentManager | None = None
        self._definition = ToolDefinition(
            name="delegate_to_agent",
            description="Delegate a focused task to a reusable subagent profile.",
            input_schema={
                "type": "object",
                "properties": {
                    "profile": {"type": "string", "enum": profiles},
                    "prompt": {"type": "string"},
                },
                "required": ["profile", "prompt"],
            },
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def bind(self, manager: SubagentManager) -> None:
        self._manager = manager

    async def execute(self, _context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if self._manager is None:
            return ToolResult(
                self.definition.name,
                "subagent manager is unavailable",
                is_error=True,
            )
        try:
            record = await self._manager.delegate(
                str(arguments.get("profile", "")),
                str(arguments.get("prompt", "")),
            )
        except Exception as exc:
            return ToolResult(self.definition.name, str(exc), is_error=True)
        return ToolResult(
            self.definition.name,
            f"subagent {record.id} ({record.profile}) completed:\n{record.result}",
        )
