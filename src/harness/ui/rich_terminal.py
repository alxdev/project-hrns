from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from harness.agent.loop import TextEvent, ToolCallEvent, ToolResultEvent
from harness.commands.registry import CommandContext, CommandRegistry
from harness.api.types import ToolDefinition
from harness.ui.terminal import TerminalUI


class RichTerminalUI(TerminalUI):
    """Rich presentation and command handling layered over the basic terminal UI."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    async def emit(self, event: object) -> None:
        if isinstance(event, TextEvent):
            self.console.print(Markdown(event.text))
        elif isinstance(event, ToolCallEvent):
            arguments = json.dumps(event.arguments, indent=2, sort_keys=True)
            self.console.print(Panel(arguments, title=f"tool · {event.name}", border_style="yellow"))
        elif isinstance(event, ToolResultEvent):
            style = "red" if event.result.is_error else "dim"
            self.console.print(
                Panel(event.result.content, title="tool error" if event.result.is_error else "tool result", style=style)
            )

    async def approve(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        prompt = f"Approve {tool.name} {json.dumps(arguments, sort_keys=True)}? [y/N] "
        answer = await asyncio.to_thread(self.console.input, prompt)
        return answer.strip().lower() in {"y", "yes"}

    async def run(
        self,
        agent,
        commands: CommandRegistry,
        command_context: CommandContext,
    ) -> None:
        while True:
            try:
                prompt = await asyncio.to_thread(self.console.input, "[bold cyan]❯[/bold cyan] ")
            except EOFError:
                self.console.print()
                return
            if not prompt.strip():
                continue

            command = commands.dispatch(prompt, command_context)
            if command is not None:
                if command.text:
                    self.console.print(command.text, markup=False)
                if command.should_exit:
                    return
                continue

            try:
                with self.console.status("Thinking...", spinner="dots"):
                    await agent.send(prompt)
            except Exception as exc:
                self.console.print(f"[red]error:[/red] {exc}", file=sys.stderr)

