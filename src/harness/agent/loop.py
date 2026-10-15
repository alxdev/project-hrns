from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.api.types import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCall,
    ToolDefinition,
    ToolResult,
    UserMessage,
)
from harness.compaction import CompactionManager
from harness.permissions.policies import PermissionDecision, PermissionPolicy
from harness.provider.base import Provider
from harness.tools.base import ToolContext
from harness.tools.registry import ToolRegistry


class EventSink(Protocol):
    async def emit(self, event: object) -> None: ...


class NullEventSink:
    async def emit(self, event: object) -> None:
        return None


@dataclass(frozen=True)
class UserMessageEvent:
    text: str
    agent: str = ""


@dataclass(frozen=True)
class TextEvent:
    text: str
    agent: str = ""
    log_prefix: str = ""


@dataclass(frozen=True)
class TextDeltaEvent:
    text: str
    agent: str = ""
    log_prefix: str = ""


@dataclass(frozen=True)
class TextStreamCompletedEvent:
    agent: str = ""


@dataclass(frozen=True)
class ToolCallEvent:
    name: str
    arguments: dict[str, Any]
    agent: str = ""
    log_prefix: str = ""
    tool_call_id: str = ""


@dataclass(frozen=True)
class ToolResultEvent:
    name: str
    result: ToolResult
    agent: str = ""
    log_prefix: str = ""


@dataclass(frozen=True)
class SubagentLifecycleEvent:
    profile: str
    record_id: str
    phase: str
    elapsed_seconds: float | None = None


@dataclass
class Agent:
    provider: Provider
    tools: ToolRegistry
    permissions: PermissionPolicy
    context: ToolContext
    events: EventSink = field(default_factory=NullEventSink)
    compaction: CompactionManager | None = None
    messages: list[Message] = field(default_factory=list)
    max_turns: int = 20
    name: str = ""
    system_prompt: str | None = None
    log_prefix: str = ""
    quiet: bool = False

    async def send(self, prompt: str) -> str:
        self.messages.append(UserMessage(prompt))
        await self.events.emit(UserMessageEvent(prompt, self.name))
        final_text = ""
        for _ in range(self.max_turns):
            if self.compaction is not None:
                self.messages = await self.compaction.compact(self.messages)
            response = await self.provider.send(self.messages, self.tools.definitions())
            self.messages.append(AssistantMessage(response.content))
            text = "".join(block.text for block in response.content if isinstance(block, TextBlock))
            if text:
                final_text = text
                if not self.quiet:
                    await self.events.emit(TextEvent(text, self.name, self.log_prefix))

            calls = [block for block in response.content if isinstance(block, ToolCall)]
            if not calls:
                return final_text

            results: list[ToolResult] = []
            for call in calls:
                await self.events.emit(
                    ToolCallEvent(
                        call.name,
                        call.input,
                        self.name,
                        self.log_prefix,
                        call.id,
                    )
                )
                definition = next(
                    (item for item in self.tools.definitions() if item.name == call.name),
                    ToolDefinition(call.name, "", {"type": "object"}),
                )
                decision = await self.permissions.decide(definition, call.input)
                if decision is PermissionDecision.DENY:
                    result = ToolResult(call.id, "user denied this tool call", is_error=True)
                else:
                    result = await self.tools.execute(self.context, call.name, call.input)
                    result = ToolResult(call.id, result.content, result.is_error)
                results.append(result)
                await self.events.emit(
                    ToolResultEvent(call.name, result, self.name, self.log_prefix)
                )
            self.messages.append(Message(role="user", content=results))
        raise RuntimeError(f"agent exceeded maximum turns ({self.max_turns})")
