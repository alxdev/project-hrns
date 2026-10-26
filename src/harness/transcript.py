from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from harness.agent.loop import (
    EventSink,
    MCPDiscoveryEvent,
    SubagentLifecycleEvent,
    TextDeltaEvent,
    TextEvent,
    TextStreamCompletedEvent,
    TextStreamInterruptedEvent,
    ToolCallEvent,
    ToolExecutionEvent,
    ToolLogEvent,
    ToolResultEvent,
    UserMessageEvent,
)


@dataclass
class TranscriptEntry:
    """One user-visible or operational item in a conversation transcript."""

    id: str
    kind: str
    content: str
    agent: str = ""
    status: str = "complete"
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class TranscriptUpdateEvent:
    """A detached transcript change forwarded to UI output adapters."""

    source_event: object
    entry: TranscriptEntry | None
    is_new: bool
    activity: str | None


class Transcript:
    """In-memory transcript state reduced from framework-neutral agent events."""

    def __init__(self) -> None:
        self._entries: list[TranscriptEntry] = []
        self._tool_entries: dict[str, TranscriptEntry] = {}
        self._subagent_entries: dict[str, TranscriptEntry] = {}
        self._mcp_entries: dict[tuple[str, str], TranscriptEntry] = {}
        self._streaming_assistant_entries: dict[str, TranscriptEntry] = {}
        self.activity: str | None = None

    def apply(self, event: object) -> tuple[TranscriptEntry | None, bool]:
        if isinstance(event, UserMessageEvent):
            self.activity = (
                f"Working on delegated task: {event.agent}..."
                if event.agent and event.agent != "root"
                else "Thinking..."
            )
            return self._append("user", event.text, event.agent), True
        elif isinstance(event, TextEvent):
            streamed_entry = self._streaming_assistant_entries.get(event.agent)
            if streamed_entry is None:
                self.activity = None
                return self._append("assistant", event.text, event.agent), True
            else:
                streamed_entry.content = event.text or streamed_entry.content
                self.activity = None
                return self.complete_text_stream(event.agent), False
        elif isinstance(event, TextDeltaEvent):
            is_new = event.agent not in self._streaming_assistant_entries
            self.activity = None
            return self.append_text_delta(event.agent, event.text), is_new
        elif isinstance(event, TextStreamCompletedEvent):
            self.activity = None
            return self.complete_text_stream(event.agent), False
        elif isinstance(event, TextStreamInterruptedEvent):
            self.activity = None
            return self.interrupt_text_stream(event.agent), False
        elif isinstance(event, ToolCallEvent):
            self.activity = f"Checking tool: {event.name}..."
            entry = self._append(
                "tool_call",
                event.name,
                event.agent,
                status="checking",
                metadata={
                    "arguments": event.arguments,
                    "tool_call_id": event.tool_call_id,
                    "log_prefix": event.log_prefix,
                    "phase": "checking permission",
                },
            )
            if event.tool_call_id:
                self._tool_entries[event.tool_call_id] = entry
            return entry, True
        elif isinstance(event, ToolExecutionEvent):
            execution_entry = self._tool_entries.get(event.tool_call_id)
            if execution_entry is not None:
                execution_entry.status = "running"
                execution_entry.metadata["phase"] = "running"
                execution_entry.updated_at = datetime.now()
            self.activity = f"Running tool: {event.name}..."
            return execution_entry, False
        elif isinstance(event, ToolResultEvent):
            call_entry = self._tool_entries.get(event.result.tool_use_id)
            if call_entry is not None:
                call_entry.status = "failed" if event.result.is_error else "completed"
                call_entry.metadata["result"] = event.result.content
                call_entry.updated_at = datetime.now()
                self.activity = "Thinking..."
                return call_entry, False
            else:
                self.activity = "Thinking..."
                return self._append(
                    "tool_result",
                    event.result.content,
                    event.agent,
                    status="failed" if event.result.is_error else "completed",
                    metadata={"name": event.name, "tool_call_id": event.result.tool_use_id},
                ), True
        elif isinstance(event, ToolLogEvent):
            self.activity = f"Running tool: {event.name}..."
            return self._append(
                "tool_log",
                event.text.rstrip(),
                event.agent,
                status="complete",
                metadata={
                    "name": event.name,
                    "stream": event.stream,
                    "log_prefix": event.log_prefix,
                },
            ), True
        elif isinstance(event, SubagentLifecycleEvent):
            if event.phase == "started":
                self.activity = f"Delegating to {event.profile}..."
                started_entry = self._append(
                    "subagent",
                    event.profile,
                    event.profile,
                    status="running",
                    metadata={"record_id": event.record_id},
                )
                self._subagent_entries[event.record_id] = started_entry
                return started_entry, True
            else:
                completed_entry = self._subagent_entries.get(event.record_id)
                if completed_entry is not None:
                    completed_entry.status = event.phase
                    completed_entry.updated_at = datetime.now()
                    completed_entry.metadata["elapsed_seconds"] = event.elapsed_seconds
                self.activity = "Thinking..."
                return completed_entry, False
        elif isinstance(event, MCPDiscoveryEvent):
            key = (event.profile, event.server)
            if event.phase == "started":
                self.activity = f"{event.profile} is discovering {event.server} MCP tools..."
                entry = self._append(
                    "mcp_discovery",
                    self.activity,
                    event.profile,
                    status="running",
                    metadata={"server": event.server},
                )
                self._mcp_entries[key] = entry
                return entry, True
            completed_entry = self._mcp_entries.get(key)
            if completed_entry is not None:
                if event.phase == "completed":
                    completed_entry.content = (
                        f"{event.server} MCP tools loaded ({event.tool_count} tools) "
                        f"for {event.profile}"
                    )
                    completed_entry.status = "completed"
                else:
                    completed_entry.content = (
                        f"Failed to load {event.server} MCP tools for {event.profile}: "
                        f"{event.error or 'unknown error'}"
                    )
                    completed_entry.status = "failed"
                completed_entry.updated_at = datetime.now()
            self.activity = "Thinking..."
            return completed_entry, False
        return None, False

    def append_text_delta(self, agent: str, text: str) -> TranscriptEntry:
        """Append a future streaming text delta to the active assistant entry."""
        entry = self._streaming_assistant_entries.get(agent)
        if entry is None:
            entry = self._append("assistant", "", agent, status="streaming")
            self._streaming_assistant_entries[agent] = entry
        entry.content += text
        entry.updated_at = datetime.now()
        return entry

    def complete_text_stream(self, agent: str) -> TranscriptEntry | None:
        entry = self._streaming_assistant_entries.pop(agent, None)
        if entry is not None:
            entry.status = "complete"
            entry.updated_at = datetime.now()
        return entry

    def interrupt_text_stream(self, agent: str) -> TranscriptEntry | None:
        entry = self._streaming_assistant_entries.pop(agent, None)
        if entry is not None:
            entry.status = "interrupted"
            entry.updated_at = datetime.now()
        return entry

    def entries(self) -> tuple[TranscriptEntry, ...]:
        """Return a detached snapshot of the current entries."""
        return tuple(deepcopy(self._entries))

    def clear(self) -> None:
        self._entries.clear()
        self._tool_entries.clear()
        self._subagent_entries.clear()
        self._mcp_entries.clear()
        self._streaming_assistant_entries.clear()
        self.activity = None

    def _append(
        self,
        kind: str,
        content: str,
        agent: str = "",
        *,
        status: str = "complete",
        metadata: dict[str, object] | None = None,
    ) -> TranscriptEntry:
        now = datetime.now()
        entry = TranscriptEntry(
            id=uuid4().hex,
            kind=kind,
            content=content,
            agent=agent,
            status=status,
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        self._entries.append(entry)
        return entry


class TranscriptEventSink:
    """Record events in a transcript, then forward them to another event sink."""

    def __init__(self, transcript: Transcript, downstream: EventSink) -> None:
        self.transcript = transcript
        self.downstream = downstream

    async def emit(self, event: object) -> None:
        entry, is_new = self.transcript.apply(event)
        snapshot = deepcopy(entry) if entry is not None else None
        await self.downstream.emit(
            TranscriptUpdateEvent(
                source_event=event,
                entry=snapshot,
                is_new=is_new,
                activity=self.transcript.activity,
            )
        )
