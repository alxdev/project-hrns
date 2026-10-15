from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from harness.agent.loop import (
    EventSink,
    SubagentLifecycleEvent,
    TextDeltaEvent,
    TextEvent,
    TextStreamCompletedEvent,
    ToolCallEvent,
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


class Transcript:
    """In-memory transcript state reduced from framework-neutral agent events."""

    def __init__(self) -> None:
        self._entries: list[TranscriptEntry] = []
        self._tool_entries: dict[str, TranscriptEntry] = {}
        self._subagent_entries: dict[str, TranscriptEntry] = {}
        self._streaming_assistant_entries: dict[str, TranscriptEntry] = {}

    def apply(self, event: object) -> None:
        if isinstance(event, UserMessageEvent):
            self._append("user", event.text, event.agent)
        elif isinstance(event, TextEvent):
            streamed_entry = self._streaming_assistant_entries.get(event.agent)
            if streamed_entry is None:
                self._append("assistant", event.text, event.agent)
            else:
                streamed_entry.content = event.text or streamed_entry.content
                self.complete_text_stream(event.agent)
        elif isinstance(event, TextDeltaEvent):
            self.append_text_delta(event.agent, event.text)
        elif isinstance(event, TextStreamCompletedEvent):
            self.complete_text_stream(event.agent)
        elif isinstance(event, ToolCallEvent):
            entry = self._append(
                "tool_call",
                event.name,
                event.agent,
                status="running",
                metadata={
                    "arguments": event.arguments,
                    "tool_call_id": event.tool_call_id,
                    "log_prefix": event.log_prefix,
                },
            )
            if event.tool_call_id:
                self._tool_entries[event.tool_call_id] = entry
        elif isinstance(event, ToolResultEvent):
            call_entry = self._tool_entries.get(event.result.tool_use_id)
            if call_entry is not None:
                call_entry.status = "failed" if event.result.is_error else "completed"
                call_entry.metadata["result"] = event.result.content
                call_entry.updated_at = datetime.now()
            else:
                self._append(
                    "tool_result",
                    event.result.content,
                    event.agent,
                    status="failed" if event.result.is_error else "completed",
                    metadata={"name": event.name, "tool_call_id": event.result.tool_use_id},
                )
        elif isinstance(event, SubagentLifecycleEvent):
            if event.phase == "started":
                entry = self._append(
                    "subagent",
                    event.profile,
                    event.profile,
                    status="running",
                    metadata={"record_id": event.record_id},
                )
                self._subagent_entries[event.record_id] = entry
            else:
                entry = self._subagent_entries.get(event.record_id)
                if entry is not None:
                    entry.status = event.phase
                    entry.updated_at = datetime.now()
                    entry.metadata["elapsed_seconds"] = event.elapsed_seconds

    def append_text_delta(self, agent: str, text: str) -> TranscriptEntry:
        """Append a future streaming text delta to the active assistant entry."""
        entry = self._streaming_assistant_entries.get(agent)
        if entry is None:
            entry = self._append("assistant", "", agent, status="streaming")
            self._streaming_assistant_entries[agent] = entry
        entry.content += text
        entry.updated_at = datetime.now()
        return entry

    def complete_text_stream(self, agent: str) -> None:
        entry = self._streaming_assistant_entries.pop(agent, None)
        if entry is not None:
            entry.status = "complete"
            entry.updated_at = datetime.now()

    def entries(self) -> tuple[TranscriptEntry, ...]:
        """Return a detached snapshot of the current entries."""
        return tuple(deepcopy(self._entries))

    def clear(self) -> None:
        self._entries.clear()
        self._tool_entries.clear()
        self._subagent_entries.clear()
        self._streaming_assistant_entries.clear()

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
        self.transcript.apply(event)
        await self.downstream.emit(event)
