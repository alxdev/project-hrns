"""Invocation-local event observer; never interpret model or tool response text."""

from __future__ import annotations

from harness.agent.loop import (
    EventSink,
    SkillActivatedEvent,
    ToolCallEvent,
    ToolExecutionEvent,
    ToolPermissionDeniedEvent,
    ToolResultEvent,
)
from harness.tools.registry import ToolRegistry

from .models import SubagentError, SubagentRecord, ToolUsage
from .outcomes import add_error


class InvocationObserver:
    def __init__(self, record: SubagentRecord, downstream: EventSink, tools: ToolRegistry) -> None:
        self.record = record
        self.downstream = downstream
        self.tools = tools
        self._known_tools = set(tools.names())
        self._denied: set[str] = set()
        record.resource_usage.observation_status = "observed"

    def observe_tools(self) -> None:
        current = set(self.tools.names())
        self.record.resource_usage.tools_added.update(current - self._known_tools)
        self._known_tools.update(current)

    async def emit(self, event: object) -> None:
        usage = self.record.resource_usage
        if isinstance(
            event, (ToolCallEvent, ToolExecutionEvent, ToolPermissionDeniedEvent, ToolResultEvent)
        ):
            counts = usage.tools.setdefault(event.name, ToolUsage())
            if isinstance(event, ToolCallEvent):
                counts.requested += 1
            elif isinstance(event, ToolExecutionEvent):
                counts.execution_started += 1
            elif isinstance(event, ToolPermissionDeniedEvent):
                counts.permission_denied += 1
                self._denied.add(event.tool_call_id)
                add_error(
                    self.record,
                    SubagentError(
                        "permission", "permission_denied", "Tool call denied by policy.", event.name
                    ),
                )
            elif isinstance(event, ToolResultEvent):
                if event.result.is_error:
                    counts.returned_error += 1
                    if event.result.tool_use_id not in self._denied:
                        add_error(
                            self.record,
                            SubagentError(
                                "tool_result",
                                "tool_error",
                                "Tool returned an error; see the invocation transcript.",
                                event.name,
                            ),
                        )
                else:
                    counts.returned_success += 1
                self._denied.discard(event.result.tool_use_id)
                self.observe_tools()
        elif isinstance(event, SkillActivatedEvent):
            usage.skills_activated[event.name] = usage.skills_activated.get(event.name, 0) + 1
        await self.downstream.emit(event)
