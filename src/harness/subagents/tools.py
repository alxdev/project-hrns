from __future__ import annotations

from dataclasses import replace
from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import ToolContext

from .manager import SubagentManager
from .models import SubagentStatus
from .outcomes import rejected_outcome, serialize_outcome


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
        if self._manager is None or self.profile not in self._manager.profiles:
            return self._definition
        return replace(
            self._definition,
            description=self._manager.capability_description(self.profile),
        )

    def bind(self, manager: SubagentManager) -> None:
        self._manager = manager

    async def execute(self, _context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        task = str(arguments.get("task", ""))
        if self._manager is None:
            return ToolResult(
                self.definition.name,
                rejected_outcome(
                    self.profile, task, RuntimeError("subagent manager is unavailable")
                ),
                is_error=True,
            )
        try:
            record = await self._manager.delegate(
                self.profile,
                task,
                capture_errors=True,
            )
        except Exception as exc:
            return ToolResult(
                self.definition.name, rejected_outcome(self.profile, task, exc), is_error=True
            )
        return ToolResult(
            self.definition.name,
            serialize_outcome(record),
            is_error=record.status is not SubagentStatus.COMPLETED,
        )
