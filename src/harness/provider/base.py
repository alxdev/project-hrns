from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from harness.api.types import Message, ProviderResponse, ToolDefinition


@dataclass(frozen=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True)
class ProviderResponseCompleted:
    response: ProviderResponse


ProviderStreamEvent = ProviderTextDelta | ProviderResponseCompleted


class Provider(Protocol):
    @property
    def model(self) -> str: ...

    async def send(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> ProviderResponse: ...


class StreamingProvider(Protocol):
    """Optional provider capability for incremental text delivery."""

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderStreamEvent]: ...
