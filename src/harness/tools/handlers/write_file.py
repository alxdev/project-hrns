from __future__ import annotations

from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    path = context.working_directory / arguments["path"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arguments["content"], encoding="utf-8")
        return ToolResult("", f"wrote {path}")
    except Exception as exc:
        return ToolResult("", str(exc), is_error=True)
