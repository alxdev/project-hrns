from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import ToolContext


@dataclass
class MCPTool:
    client_factory: Callable[[str], AbstractAsyncContextManager[Any]]
    url: str
    server_name: str
    remote_name: str
    definition: ToolDefinition

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        del context
        async with self.client_factory(self.url) as client:
            result = await client.call_tool(self.remote_name, arguments)
        parts: list[str] = []
        for item in getattr(result, "content", ()) or ():
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
            else:
                kind = getattr(item, "type", "content")
                parts.append(f"[{kind} content returned by {self.server_name}]")
        structured = getattr(result, "structured_content", None)
        if not parts and structured is not None:
            parts.append(json.dumps(structured, ensure_ascii=False, default=str))
        if not parts:
            parts.append("MCP tool returned no content")
        return ToolResult(
            tool_use_id=self.definition.name,
            content="\n".join(parts),
            is_error=bool(getattr(result, "is_error", False)),
        )
