from __future__ import annotations

from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import ToolContext


class ReadFileTool:
    definition = ToolDefinition(
        name="read_file",
        description="Read a UTF-8 text file relative to the working directory.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path."}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        path = context.working_directory / arguments["path"]
        try:
            return ToolResult("", path.read_text(encoding="utf-8"))
        except Exception as exc:
            return ToolResult("", str(exc), is_error=True)

