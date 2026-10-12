from __future__ import annotations

from typing import Protocol


class Subagent(Protocol):
    """A named, configurable unit that can execute a delegated task."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    async def run(self, task: str) -> str: ...
