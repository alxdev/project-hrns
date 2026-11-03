from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status

from harness.transcript import TranscriptEntry, TranscriptUpdateEvent


class RichOutput:
    """Output adapter implemented with Rich."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._status: Status | None = None
        self._activity: str | None = None
        self._rendered_text_lengths: dict[str, int] = {}

    async def emit(self, event: object) -> None:
        if not isinstance(event, TranscriptUpdateEvent):
            return
        self._update_activity(event.activity)
        entry = event.entry
        if entry is None:
            return
        if entry.kind == "assistant":
            self._render_assistant(entry, event.is_new)
        elif entry.kind == "tool_call":
            self._render_tool(entry, event.is_new)
        elif entry.kind == "subagent":
            self._render_subagent(entry, event.is_new)
        elif entry.kind == "mcp_discovery":
            self._render_mcp_discovery(entry, event.is_new)
        elif entry.kind == "tool_log":
            self._render_tool_log(entry)

    def _update_activity(self, activity: str | None) -> None:
        self._activity = activity
        status = self._status
        if status is None:
            return
        if activity is None:
            status.stop()
            return
        status.update(activity)
        status.start()

    def _print_persistently(self, *objects: Any, **kwargs: Any) -> None:
        status = self._status
        if status is not None:
            status.stop()
        self.console.print(*objects, **kwargs)
        if status is not None and self._activity is not None:
            status.update(self._activity)
            status.start()

    def _render_assistant(self, entry: TranscriptEntry, is_new: bool) -> None:
        if entry.status == "streaming":
            previous_length = self._rendered_text_lengths.get(entry.id, 0)
            delta = entry.content[previous_length:]
            if delta:
                self._print_persistently(delta, end="", markup=False, highlight=False)
            self._rendered_text_lengths[entry.id] = len(entry.content)
            return
        rendered_length = (
            self._rendered_text_lengths.pop(entry.id)
            if entry.id in self._rendered_text_lengths
            else None
        )
        if rendered_length is not None:
            self._print_persistently()
            if entry.status == "interrupted":
                self._print_persistently("[dim]response interrupted[/dim]")
        elif is_new and entry.content:
            self._print_persistently(Markdown(entry.content))

    def _render_tool(self, entry: TranscriptEntry, is_new: bool) -> None:
        if is_new:
            arguments = json.dumps(entry.metadata.get("arguments", {}), indent=2, sort_keys=True)
            self._print_persistently(
                Panel(
                    arguments,
                    title=f"{entry.metadata.get('log_prefix', '')}tool · {entry.content}",
                    border_style="yellow",
                )
            )
        elif entry.status in {"completed", "failed"}:
            is_error = entry.status == "failed"
            self._print_persistently(
                Panel(
                    str(entry.metadata.get("result", "")),
                    title="tool error" if is_error else "tool result",
                    style="red" if is_error else "dim",
                )
            )

    def _render_subagent(self, entry: TranscriptEntry, is_new: bool) -> None:
        if is_new:
            record_id = entry.metadata.get("record_id", "")
            self._print_persistently(f"[dim]↳ delegating to {entry.content} ({record_id})[/dim]")
            return
        elapsed = entry.metadata.get("elapsed_seconds", 0.0)
        elapsed_seconds = elapsed if isinstance(elapsed, (int, float)) else 0.0
        self._print_persistently(
            f"[dim]← {entry.content} {entry.status} ({elapsed_seconds:.1f}s)[/dim]"
        )

    def _render_mcp_discovery(self, entry: TranscriptEntry, is_new: bool) -> None:
        if is_new:
            return
        style = "red" if entry.status == "failed" else "dim"
        self._print_persistently(f"[{style}]{entry.content}[/{style}]")

    def _render_tool_log(self, entry: TranscriptEntry) -> None:
        prefix = entry.metadata.get("log_prefix", "")
        self._print_persistently(
            f"{prefix}{entry.content}", style="dim", markup=False, highlight=False
        )

    def write(self, text: str) -> None:
        self.console.print(text, markup=False)

    def error(self, text: str) -> None:
        self.console.print(f"[red]error:[/red] {text}")

    @asynccontextmanager
    async def thinking(self) -> AsyncIterator[None]:
        status = self.console.status("Thinking...", spinner="dots")
        self._status = status
        try:
            with status:
                yield
        finally:
            self._status = None
            self._activity = None

    @contextmanager
    def suspend(self) -> Iterator[None]:
        status = self._status
        if status is not None:
            status.stop()
        try:
            yield
        finally:
            if status is not None:
                status.start()
