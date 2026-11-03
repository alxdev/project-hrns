from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field, replace
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
from harness.tools.results import capability_result


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
class ToolLogEvent:
    name: str
    text: str
    stream: str = "stderr"
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

    def __post_init__(self) -> None:
        self.context = replace(self.context, tool_registry=self.tools)

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
        assignment = UserMessage(prompt)
        self.messages.append(assignment)
        if not self.quiet:
            await self.events.emit(UserMessageEvent(prompt, self.name))
        final_text = ""
        for _ in range(self.max_turns):
            if self.compaction is not None:
                self.messages = await self.compaction.compact(self.messages)
            response, streamed = await self._request_response(assignment)
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
            activated_skills = {
                message.skill_name
                for message in self.messages
                if message.kind == "skill_activation" and message.skill_name
            }
            skill_context: list[Message] = []
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
                    result = capability_result(
                        "access_denied",
                        "user denied this tool call",
                        "Respect the denial; do not repeat the same denied request "
                        "or retry automatically.",
                        is_error=True,
                        tool_call_id=call.id,
                    )
                elif (
                    call.name == "activate_skill"
                    and isinstance(call.input.get("name"), str)
                    and call.input["name"] in activated_skills
                    and call.input["name"] in self.context.allowed_skills
                    and any(item.name == call.name for item in self.tools.definitions())
                ):
                    result = ToolResult(
                        call.id,
                        f"Skill {call.input['name']!r} is already active; "
                        "use its preserved instructions.",
                    )
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
                    if call.name == "activate_skill" and not result.is_error:
                        skill_name = str(call.input["name"])
                        skill_context.append(
                            UserMessage(
                                result.content,
                                preserve_from_compaction=True,
                                kind="skill_activation",
                                skill_name=skill_name,
                            )
                        )
                        activated_skills.add(skill_name)
                        result = ToolResult(
                            call.id,
                            f"Activated skill {skill_name!r}; "
                            "its full instructions follow in preserved context.",
                        )
                results.append(result)
                await self.events.emit(
                    ToolResultEvent(call.name, result, self.name, self.log_prefix)
                )
            self.messages.append(Message(role="user", content=results))
            self.messages.extend(skill_context)
        raise RuntimeError(f"agent exceeded maximum turns ({self.max_turns})")

    async def _request_response(self, assignment: Message) -> tuple[ProviderResponse, bool]:
        messages = list(self.messages)
        # Keep this invocation's exact task available even if history was trimmed or
        # summarized. This local reference never pins assignments in reused history.
        if not any(message is assignment for message in messages):
            messages.insert(0, assignment)
        stream = getattr(self.provider, "stream", None)
        if not self.streaming or not callable(stream):
            return await self.provider.send(messages, self.tools.definitions()), False

        response = None
        try:
            async with aclosing(stream(messages, self.tools.definitions())) as events:
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
