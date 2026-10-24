from __future__ import annotations

from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    path = context.working_directory / arguments["path"]
    try:
        return ToolResult("", path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ToolResult("", str(exc), is_error=True)
