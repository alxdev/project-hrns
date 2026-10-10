from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from harness.api.types import Message, ProviderResponse, ToolDefinition


class MockProvider:
    def __init__(
        self,
        responses: Sequence[ProviderResponse] | None = None,
        responder: Callable[
            [Sequence[Message], Sequence[ToolDefinition]], Awaitable[ProviderResponse]
        ]
        | None = None,
        model: str = "mock-model",
    ) -> None:
        self.responses = list(responses or [])
        self.responder = responder
        self._model = model
        self.calls: list[tuple[list[Message], list[ToolDefinition]]] = []

    @property
    def model(self) -> str:
        return self._model

    def set_model(self, model: str) -> None:
        self._model = model

    async def send(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> ProviderResponse:
        self.calls.append((list(messages), list(tools)))
        if self.responder:
            return await self.responder(messages, tools)
        if not self.responses:
            raise AssertionError("MockProvider has no response remaining")
        return self.responses.pop(0)
