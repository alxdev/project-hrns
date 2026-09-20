from __future__ import annotations

from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import ToolContext


class WriteFileTool:
    definition = ToolDefinition(
        name="write_file",
        description="Write UTF-8 text to a file relative to the working directory.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "content": {"type": "string", "description": "Text to write."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        path = context.working_directory / arguments["path"]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(arguments["content"], encoding="utf-8")
            return ToolResult("", f"wrote {path}")
        except Exception as exc:
            return ToolResult("", str(exc), is_error=True)

