from __future__ import annotations

from dataclasses import replace
from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import ToolContext

from .manager import SubagentManager
from .models import SubagentStatus
from .outcomes import rejected_outcome, serialize_outcome


class DelegationTool:
    """One routing tool for every currently configured subagent profile."""

    def __init__(self) -> None:
        self._manager: SubagentManager | None = None
        self._definition = ToolDefinition(
            name="delegate_subagent",
            description="Delegate a focused task to one configured subagent profile.",
            input_schema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Exact configured subagent profile name "
                        "from the catalog below.",
                    },
                    "task": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The complete bounded outcome for this profile, including "
                        "the user's requested scope, exact targets, inputs, constraints, and "
                        "verification only as needed and proportional to establish the requested "
                        "outcome, without extra deliverables or reporting requirements. "
                        "Identify skills separately from callable tools. Keep "
                        "ordinary intermediate discovery and harness approvals in this "
                        "assignment; use discovery-only scope only when that is the outcome.",
                    },
                },
                "required": ["profile", "task"],
                "additionalProperties": False,
            },
        )

    @property
    def definition(self) -> ToolDefinition:
        profiles = sorted(self._manager.profiles) if self._manager is not None else []
        schema = self._definition.input_schema
        profile_schema = dict(schema["properties"]["profile"])
        if profiles:
            profile_schema["enum"] = profiles
        catalog = (
            "\n\n".join(
                f"Profile {name}:\n{self._manager.capability_description(name)}"
                for name in profiles
            )
            if self._manager is not None and profiles
            else "No subagent profiles are configured."
        )
        return replace(
            self._definition,
            description=self._definition.description + "\n\n" + catalog,
            input_schema={
                **schema,
                "properties": {**schema["properties"], "profile": profile_schema},
            },
        )

    def bind(self, manager: SubagentManager) -> None:
        self._manager = manager

    async def execute(self, _context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        profile = arguments.get("profile", "")
        task = arguments.get("task", "")
        try:
            if set(arguments) - {"profile", "task"}:
                raise ValueError("delegate_subagent accepts only profile and task")
            if not isinstance(profile, str) or not profile.strip():
                raise ValueError("profile must be a non-empty configured subagent name")
            if not isinstance(task, str) or not task.strip():
                raise ValueError("task must be a non-empty string")
            if self._manager is None:
                raise RuntimeError("subagent manager is unavailable")
            record = await self._manager.delegate(
                profile,
                task,
                capture_errors=True,
            )
        except Exception as exc:
            return ToolResult(
                self._definition.name,
                rejected_outcome(
                    profile if isinstance(profile, str) else "",
                    task if isinstance(task, str) else "",
                    exc,
                ),
                is_error=True,
            )
        return ToolResult(
            self._definition.name,
            serialize_outcome(record),
            is_error=record.status is not SubagentStatus.COMPLETED,
        )
