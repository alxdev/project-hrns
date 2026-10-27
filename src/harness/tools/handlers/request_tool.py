from __future__ import annotations

from typing import Any

from harness.api.types import ToolResult
from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import ToolContext
from harness.tools.results import capability_result


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    reason = arguments.get("reason")
    if not isinstance(name, str) or not name.strip():
        return capability_result(
            "validation_error",
            "requested tool name must be a non-empty string",
            "Supply a valid installed tool name.",
            is_error=True,
        )
    if not isinstance(reason, str) or not reason.strip():
        return capability_result(
            "validation_error",
            "tool request reason must be a non-empty string",
            "Supply a specific reason.",
            is_error=True,
        )
    registry = context.tool_registry
    if registry is None or context.tool_catalog is None:
        return capability_result(
            "configuration_error",
            "tool access is not configured for this agent",
            "Report the configuration issue.",
            is_error=True,
        )
    existing = registry.get(name)
    if existing is not None and not isinstance(existing, ArtifactTool):
        return capability_result(
            "already_available",
            f"tool {name!r} is already available",
            "Use the tool subject to execution policy.",
        )
    try:
        tool = context.tool_catalog.get(name)
    except InvalidArtifactError as exc:
        if isinstance(existing, ArtifactTool):
            registry.unregister(name)
        return capability_result(
            "existing_artifact_invalid",
            str(exc),
            "Use tool_authoring with replace_invalid=true and a complete replacement; "
            "separate explicit user approval is required. Do not retry automatically.",
            is_error=True,
            name=exc.name,
            path=str(exc.path),
            reason=exc.reason,
        )
    except (OSError, ValueError) as exc:
        return capability_result(
            "load_failed",
            f"cannot load tool {name!r}: {exc}",
            "Report the installed tool load failure; do not recreate it.",
            is_error=True,
        )
    if tool is None:
        if isinstance(existing, ArtifactTool):
            registry.unregister(name)
        next_step = "Report the missing capability; do not create a tool automatically."
        try:
            authoring = context.tool_catalog.get("tool_authoring")
        except (OSError, ValueError):
            authoring = None
        if authoring is not None:
            action = (
                "use tool_authoring"
                if registry.get("tool_authoring") is not None
                else f"{context.tool_name or 'request_tool'} for tool_authoring"
            )
            next_step += (
                f" Only for explicit, authorized, narrowly scoped supplementary creation, {action}."
            )
        return capability_result(
            "not_installed",
            (
                f"tool artifact {name!r} at {existing.directory}: missing artifact directory"
                if isinstance(existing, ArtifactTool)
                else f"tool {name!r} is not installed in this harness"
            ),
            next_step,
            is_error=True,
        )
    if existing is not None:
        # Valid definitions stay fixed within this delegation. Only invalid or
        # missing artifacts revoke a grant here; normal refresh is at delegation.
        return capability_result(
            "already_available",
            f"tool {name!r} is already available",
            "Use the tool subject to execution policy.",
        )
    registry.register(tool)
    persisted = await context.persist_tool_access(name) if context.persist_tool_access else False
    persistence_note = " It was also saved to the subagent profile." if persisted else ""
    return capability_result(
        "enabled",
        f"tool {name!r} is now available for this subagent session. "
        f"Retry the operation.{persistence_note}",
        "Use the enabled tool subject to execution policy.",
    )
