from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status

from harness.agent.loop import (
    SubagentLifecycleEvent,
    TextDeltaEvent,
    TextEvent,
    TextStreamCompletedEvent,
    TextStreamInterruptedEvent,
    ToolCallEvent,
    ToolResultEvent,
)


class RichOutput:
    """Output adapter implemented with Rich."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._status: Status | None = None

    async def emit(self, event: object) -> None:
        if isinstance(event, TextEvent):
            self.console.print(Markdown(event.text))
        elif isinstance(event, TextDeltaEvent):
            # Rich's Status uses a transient Live display that restores its
            # previous screen contents when stopped. End it before printing
            # streamed output so the response remains in the terminal.
            if self._status is not None:
                self._status.stop()
                self._status = None
            self.console.print(event.text, end="", markup=False, highlight=False)
        elif isinstance(event, (TextStreamCompletedEvent, TextStreamInterruptedEvent)):
            self.console.print()
        elif isinstance(event, ToolCallEvent):
            arguments = json.dumps(event.arguments, indent=2, sort_keys=True)
            prefix = event.log_prefix
            self.console.print(
                Panel(
                    arguments,
                    title=f"{prefix}tool · {event.name}",
                    border_style="yellow",
                )
            )
        elif isinstance(event, ToolResultEvent):
            is_error = event.result.is_error
            self.console.print(
                Panel(
                    event.result.content,
                    title=f"{event.log_prefix}{'tool error' if is_error else 'tool result'}",
                    style="red" if is_error else "dim",
                )
            )
        elif isinstance(event, SubagentLifecycleEvent):
            if event.phase == "started":
                self.console.print(
                    f"[dim]↳ delegating to {event.profile} ({event.record_id})[/dim]"
                )
            else:
                elapsed = event.elapsed_seconds or 0
                self.console.print(f"[dim]← {event.profile} {event.phase} ({elapsed:.1f}s)[/dim]")

    def write(self, text: str) -> None:
        self.console.print(text, markup=False)

    def error(self, text: str) -> None:
        self.console.print(f"[red]error:[/red] {text}")

    @asynccontextmanager
    async def thinking(self) -> AsyncIterator[None]:
        status = self.console.status("Thinking...", spinner="dots")
        self._status = status
        with status:
            yield
        self._status = None

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
