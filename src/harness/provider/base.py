from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from harness.api.types import Message, ProviderResponse, ToolDefinition


class Provider(Protocol):
    @property
    def model(self) -> str: ...

    async def send(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> ProviderResponse: ...
