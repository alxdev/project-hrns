from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

Role = Literal["user", "assistant"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "unknown"]


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock: TypeAlias = TextBlock | ToolCall | ToolResult


@dataclass(frozen=True)
class Message:
    role: Role
    content: Sequence[ContentBlock] = field(default_factory=list)
    preserve_from_compaction: bool = False
    kind: str = ""
    skill_name: str = ""


def UserMessage(
    text: str,
    *,
    preserve_from_compaction: bool = False,
    kind: str = "",
    skill_name: str = "",
) -> Message:
    return Message(
        role="user",
        content=[TextBlock(text)],
        preserve_from_compaction=preserve_from_compaction,
        kind=kind,
        skill_name=skill_name,
    )


def AssistantMessage(
    content: Sequence[ContentBlock],
    *,
    preserve_from_compaction: bool = False,
    kind: str = "",
) -> Message:
    return Message(
        role="assistant",
        content=list(content),
        preserve_from_compaction=preserve_from_compaction,
        kind=kind,
    )


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ProviderResponse:
    content: Sequence[ContentBlock]
    stop_reason: StopReason
