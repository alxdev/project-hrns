from __future__ import annotations

from typing import Protocol, Sequence

from harness.api.types import Message, ProviderResponse, ToolDefinition


class Provider(Protocol):
    @property
    def model(self) -> str: ...

    async def send(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> ProviderResponse: ...

