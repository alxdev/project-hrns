from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Any, Protocol

from harness.api.types import ToolDefinition
from harness.model_selection import ModelOption


class InputAdapter(Protocol):
    async def read(self) -> str: ...

    async def approve(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool: ...

    async def prompt_secret(self, variable: str) -> str: ...

    async def pick_model(
        self,
        options: Sequence[ModelOption],
        current: str | None = None,
    ) -> str | None: ...


class OutputRenderer(Protocol):
    async def emit(self, event: object) -> None: ...

    def write(self, text: str) -> None: ...

    def error(self, text: str) -> None: ...

    def thinking(self) -> AbstractAsyncContextManager[None]: ...

    def suspend(self) -> AbstractContextManager[None]: ...
