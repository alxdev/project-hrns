from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from harness.api.types import ToolDefinition, ToolResult


@dataclass(frozen=True)
class ToolContext:
    working_directory: Path


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult: ...

