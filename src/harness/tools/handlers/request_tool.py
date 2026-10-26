from __future__ import annotations

from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    reason = arguments.get("reason")
    if not isinstance(name, str) or not name.strip():
        return ToolResult("", "requested tool name must be a non-empty string", True)
    if not isinstance(reason, str) or not reason.strip():
        return ToolResult("", "tool request reason must be a non-empty string", True)
    registry = context.tool_registry
    if registry is None or context.tool_catalog is None:
        return ToolResult("", "tool access is not configured for this agent", True)
    if registry.get(name) is not None:
        return ToolResult("", f"tool {name!r} is already available")
    try:
        tool = context.tool_catalog.get(name)
    except (OSError, ValueError) as exc:
        return ToolResult("", f"cannot load tool {name!r}: {exc}", True)
    if tool is None:
        return ToolResult(
            "",
            f"tool {name!r} is not installed in this harness; "
            "use tool_authoring if this subagent is authorized to create it",
            True,
        )
    registry.register(tool)
    persisted = await context.persist_tool_access(name) if context.persist_tool_access else False
    persistence_note = " It was also saved to the subagent profile." if persisted else ""
    return ToolResult(
        "",
        f"tool {name!r} is now available for this subagent session. "
        f"Retry the operation.{persistence_note}",
    )
