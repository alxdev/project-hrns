from __future__ import annotations

from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext
from harness.tools.results import capability_result


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if context.tool_catalog is None or context.tool_registry is None:
        return capability_result(
            "configuration_error",
            "tool access is not configured for this agent",
            "Report the configuration issue.",
            is_error=True,
        )
    tools = []
    errors = []
    try:
        names = tuple(context.tool_catalog)
    except (OSError, ValueError) as exc:
        return capability_result(
            "discovery_failed",
            f"cannot discover installed tools: {exc}",
            "Report the discovery failure; do not infer that tools are missing.",
            is_error=True,
        )
    for name in names:
        try:
            tool = context.tool_catalog[name]
        except (OSError, ValueError, KeyError) as exc:
            errors.append({"name": name, "message": str(exc)})
            continue
        tools.append(
            {
                "name": name,
                "description": tool.definition.description,
                "access": "available" if context.tool_registry.get(name) else "requires_approval",
            }
        )
    return capability_result(
        "listed",
        "Installed tools discovered without executing or granting access.",
        "Use assigned tools, or an available tool-access request control for an installed "
        "unassigned tool; if no request control is available, report the limitation. "
        "Access states describe session assignment, not a policy guarantee.",
        tools=tools,
        errors=errors,
    )
