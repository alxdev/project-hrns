"""Stateless replacement of one advisory snapshot after a complete tool batch."""

from __future__ import annotations

from harness.api.types import Message, TextBlock, ToolCall, ToolResult, UserMessage
from harness.tools.registry import ToolRegistry

from .tools import RecallExperience


def without_memory_snapshot(messages: list[Message]) -> list[Message]:
    return [
        message
        for message in messages
        if not (
            message.kind == "memory_snapshot"
            and message.role == "user"
            and all(isinstance(block, TextBlock) for block in message.content)
        )
    ]


class MemorySnapshot:
    def __init__(self, recall: RecallExperience, registry: ToolRegistry) -> None:
        self.recall = recall
        self.registry = registry

    def __call__(
        self, messages: list[Message], batch: list[tuple[ToolCall, ToolResult]]
    ) -> list[Message]:
        results = [
            result
            for call, result in batch
            if call.name == self.recall.definition.name
            and self.registry.get(call.name) is self.recall
            and not result.is_error
        ]
        if not results:
            return messages
        # Never mutate messages in place: Agent.send's cancellation rollback keeps
        # these same message objects. Tool exchanges and skill contexts are untouched.
        retained = without_memory_snapshot(messages)
        retained.append(
            UserMessage(
                "[Experience memory snapshot]\n" + results[-1].content,
                preserve_from_compaction=True,
                kind="memory_snapshot",
            )
        )
        return retained
