from __future__ import annotations

from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import ToolContext

from .manager import SubagentManager


class DelegationTool:
    """Tool exposed to the main agent for one named subagent profile."""

    def __init__(self, profile: str, description: str) -> None:
        self.profile = profile
        self._manager: SubagentManager | None = None
        self._definition = ToolDefinition(
            name=f"delegate_{profile}",
            description=description,
            input_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The complete bounded outcome for this profile, including "
                        "the user's requested scope, exact targets, inputs, constraints, and "
                        "verification only as needed and proportional to establish the requested "
                        "outcome, without extra deliverables or reporting requirements. "
                        "Identify skills separately from callable tools. Keep "
                        "ordinary intermediate discovery and harness approvals in this "
                        "assignment; use discovery-only scope only when that is the outcome.",
                    },
                },
                "required": ["task"],
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
                self.profile,
                str(arguments.get("task", "")),
            )
        except Exception as exc:
            return ToolResult(self.definition.name, str(exc), is_error=True)
        return ToolResult(
            self.definition.name,
            f"subagent {record.id} ({record.profile}) completed:\n{record.result}",
        )
