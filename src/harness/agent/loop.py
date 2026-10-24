from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.api.types import (
    AssistantMessage,
    Message,
    ProviderResponse,
    TextBlock,
    ToolCall,
    ToolDefinition,
    ToolResult,
    UserMessage,
)
from harness.compaction import CompactionManager
from harness.permissions.policies import PermissionDecision, PermissionPolicy
from harness.provider.base import Provider, ProviderResponseCompleted, ProviderTextDelta
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
class TextStreamInterruptedEvent:
    agent: str = ""


@dataclass(frozen=True)
class ToolCallEvent:
    name: str
    arguments: dict[str, Any]
    agent: str = ""
    log_prefix: str = ""
    tool_call_id: str = ""


@dataclass(frozen=True)
class ToolExecutionEvent:
    name: str
    tool_call_id: str
    agent: str = ""
    log_prefix: str = ""


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


@dataclass(frozen=True)
class MCPDiscoveryEvent:
    profile: str
    server: str
    phase: str
    tool_count: int = 0
    error: str = ""


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
    streaming: bool = True
    before_send: Callable[[], Awaitable[None]] | None = None

    async def send(self, prompt: str) -> str:
        messages_before_send = list(self.messages)
        try:
            return await self._send(prompt)
        except asyncio.CancelledError:
            # A cancelled operation may leave an assistant tool call without
            # its required tool result. Remove the entire incomplete turn so
            # the next request cannot send malformed provider history.
            self.messages = messages_before_send
            raise

    async def _send(self, prompt: str) -> str:
        if self.before_send is not None:
            await self.before_send()
        self.messages.append(UserMessage(prompt))
        if not self.quiet:
            await self.events.emit(UserMessageEvent(prompt, self.name))
        final_text = ""
        for _ in range(self.max_turns):
            if self.compaction is not None:
                self.messages = await self.compaction.compact(self.messages)
            response, streamed = await self._request_response()
            self.messages.append(AssistantMessage(response.content))
            text = "".join(block.text for block in response.content if isinstance(block, TextBlock))
            if text:
                final_text = text
                if not self.quiet and not streamed:
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
                    await self.events.emit(
                        ToolExecutionEvent(
                            call.name,
                            call.id,
                            self.name,
                            self.log_prefix,
                        )
                    )
                    result = await self.tools.execute(self.context, call.name, call.input)
                    result = ToolResult(call.id, result.content, result.is_error)
                results.append(result)
                await self.events.emit(
                    ToolResultEvent(call.name, result, self.name, self.log_prefix)
                )
            self.messages.append(Message(role="user", content=results))
        raise RuntimeError(f"agent exceeded maximum turns ({self.max_turns})")

    async def _request_response(self) -> tuple[ProviderResponse, bool]:
        stream = getattr(self.provider, "stream", None)
        if not self.streaming or not callable(stream):
            return await self.provider.send(self.messages, self.tools.definitions()), False

        response = None
        try:
            async with aclosing(stream(self.messages, self.tools.definitions())) as events:
                async for event in events:
                    if isinstance(event, ProviderTextDelta):
                        if not self.quiet:
                            await self.events.emit(
                                TextDeltaEvent(event.text, self.name, self.log_prefix)
                            )
                    elif isinstance(event, ProviderResponseCompleted):
                        response = event.response
        except asyncio.CancelledError:
            if not self.quiet:
                await self.events.emit(TextStreamInterruptedEvent(self.name))
            raise
        if response is None:
            raise RuntimeError("streaming provider ended without a completed response")
        if not self.quiet:
            await self.events.emit(TextStreamCompletedEvent(self.name))
        return response, True
