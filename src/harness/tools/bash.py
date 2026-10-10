from __future__ import annotations

import asyncio
from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import ToolContext


class BashTool:
    definition = ToolDefinition(
        name="bash",
        description="Run a shell command in the working directory.",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command."}},
            "required": ["command"],
            "additionalProperties": False,
        },
    )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        process = await asyncio.create_subprocess_shell(
            arguments["command"],
            cwd=context.working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        text = output.decode(errors="replace")
        if process.returncode:
            text = f"{text}\nexit code: {process.returncode}".strip()
        return ToolResult("", text, is_error=process.returncode != 0)
