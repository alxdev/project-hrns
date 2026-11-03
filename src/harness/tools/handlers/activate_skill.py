from __future__ import annotations

from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    if not isinstance(name, str) or not name.strip():
        return ToolResult("", "skill name must be a non-empty string", is_error=True)
    if context.skill_catalog is None:
        return ToolResult("", "no skills are available to this subagent", is_error=True)
    try:
        content = context.skill_catalog.activate(name, context.allowed_skills)
    except ValueError as exc:
        return ToolResult("", str(exc), is_error=True)
    return ToolResult("", content)
