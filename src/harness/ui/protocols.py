from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from harness.api.types import ToolDefinition


class InputAdapter(Protocol):
    async def read(self) -> str: ...

    async def approve(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool: ...


class OutputRenderer(Protocol):
    async def emit(self, event: object) -> None: ...

    def write(self, text: str) -> None: ...

    def error(self, text: str) -> None: ...

    def thinking(self) -> AbstractAsyncContextManager[None]: ...

