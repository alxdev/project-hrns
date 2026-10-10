from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from harness.api.types import (
    Message,
    ProviderResponse,
    TextBlock,
    ToolCall,
    ToolResult,
    UserMessage,
)
from harness.provider.base import Provider


class CompactionStrategy(Protocol):
    async def compact(self, messages: Sequence[Message]) -> list[Message]: ...


def safe_split_point(messages: Sequence[Message], desired: int) -> int:
    """Find a split that does not separate tool calls from their results."""
    if desired <= 0:
        return 0
    if desired >= len(messages):
        return len(messages)
    for index in range(desired, 0, -1):
        message = messages[index]
        has_tool_result = any(isinstance(block, ToolResult) for block in message.content)
        if not has_tool_result:
            return index
    return 0


@dataclass(frozen=True)
class NoCompaction:
    async def compact(self, messages: Sequence[Message]) -> list[Message]:
        return list(messages)


@dataclass(frozen=True)
class SlidingWindow:
    keep_last: int = 10

    async def compact(self, messages: Sequence[Message]) -> list[Message]:
        if self.keep_last < 1 or len(messages) <= self.keep_last:
            return list(messages)
        split = safe_split_point(messages, len(messages) - self.keep_last)
        return list(messages[split:]) if split else list(messages)


@dataclass
class Summarize:
    provider: Provider
    threshold: int = 20
    keep_recent: int = 6
    instructions: str = (
        "Summarize the earlier conversation for a coding assistant. Preserve file paths, "
        "commands, decisions, errors, and unfinished work. Be concise and factual."
    )

    async def compact(self, messages: Sequence[Message]) -> list[Message]:
        if len(messages) < self.threshold:
            return list(messages)
        split = safe_split_point(messages, max(1, len(messages) - self.keep_recent))
        if split == 0:
            return list(messages)
        old_messages = list(messages[:split])
        recent_messages = list(messages[split:])
        prompt = f"{self.instructions}\n\nConversation:\n{render_transcript(old_messages)}"
        response: ProviderResponse = await self.provider.send([UserMessage(prompt)], [])
        summary = "\n".join(
            block.text for block in response.content if isinstance(block, TextBlock)
        ).strip()
        if not summary:
            raise RuntimeError("summarization provider returned no text")
        return [UserMessage(f"[Earlier conversation summary]\n{summary}"), *recent_messages]


@dataclass
class LoggingCompaction:
    inner: CompactionStrategy
    path: Path

    async def compact(self, messages: Sequence[Message]) -> list[Message]:
        before = list(messages)
        after = await self.inner.compact(before)
        if len(after) != len(before):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            event = {
                "before_count": len(before),
                "after_count": len(after),
                "before": [message_to_dict(message) for message in before],
                "after": [message_to_dict(message) for message in after],
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return after


def render_transcript(messages: Sequence[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        blocks: list[str] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                blocks.append(block.text)
            elif isinstance(block, ToolCall):
                blocks.append(
                    f"[tool call: {block.name}] {json.dumps(block.input, sort_keys=True)}"
                )
            elif isinstance(block, ToolResult):
                status = "error" if block.is_error else "result"
                blocks.append(f"[tool {status}: {block.tool_use_id}] {block.content}")
        lines.append(f"{message.role}: {' '.join(blocks)}")
    return "\n".join(lines)


def message_to_dict(message: Message) -> dict[str, object]:
    content: list[dict[str, object]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolCall):
            content.append(
                {"type": "tool_call", "id": block.id, "name": block.name, "input": block.input}
            )
        elif isinstance(block, ToolResult):
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
    return {"role": message.role, "content": content}
