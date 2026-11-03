from __future__ import annotations

from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext
from harness.tools.process import run_process


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    result = await run_process(
        arguments["command"],
        cwd=context.working_directory,
        tool_name=context.tool_name or "bash",
        emit_log=context.emit_log,
    )
    return result.as_tool_result()
