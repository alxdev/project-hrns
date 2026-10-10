from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from harness.agent.loop import TextEvent, ToolCallEvent, ToolResultEvent


class RichOutput:
    """Output adapter implemented with Rich."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    async def emit(self, event: object) -> None:
        if isinstance(event, TextEvent):
            self.console.print(Markdown(event.text))
        elif isinstance(event, ToolCallEvent):
            arguments = json.dumps(event.arguments, indent=2, sort_keys=True)
            self.console.print(
                Panel(arguments, title=f"tool · {event.name}", border_style="yellow")
            )
        elif isinstance(event, ToolResultEvent):
            is_error = event.result.is_error
            self.console.print(
                Panel(
                    event.result.content,
                    title="tool error" if is_error else "tool result",
                    style="red" if is_error else "dim",
                )
            )

    def write(self, text: str) -> None:
        self.console.print(text, markup=False)

    def error(self, text: str) -> None:
        self.console.print(f"[red]error:[/red] {text}")

    @asynccontextmanager
    async def thinking(self) -> AsyncIterator[None]:
        with self.console.status("Thinking...", spinner="dots"):
            yield
