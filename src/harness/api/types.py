from __future__ import annotations

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
    content: list[ContentBlock] = field(default_factory=list)


def UserMessage(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text)])


def AssistantMessage(content: list[ContentBlock]) -> Message:
    return Message(role="assistant", content=content)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ProviderResponse:
    content: list[ContentBlock]
    stop_reason: StopReason

