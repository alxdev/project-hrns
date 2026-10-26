from __future__ import annotations

from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext, resolve_read_path


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    try:
        if "skill" in arguments and not isinstance(arguments["skill"], str):
            raise ValueError("skill must be a non-empty string")
        path = resolve_read_path(context, arguments["path"], skill=arguments.get("skill"))
        return ToolResult("", path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ToolResult("", str(exc), is_error=True)
