from __future__ import annotations

from pathlib import Path
from typing import Sequence

from rich.console import Console


def format_runtime_configuration(
    *,
    provider: str,
    model: str,
    working_directory: Path,
    compaction: str,
    tools: Sequence[str],
) -> str:
    return "\n".join(
        [
            "Runtime configuration",
            f"  provider: {provider}",
            f"  model: {model}",
            f"  directory: {working_directory}",
            f"  compaction: {compaction}",
            f"  tools: {', '.join(tools) if tools else 'none'}",
            "  approval: required before every tool call",
        ]
    )


def print_startup_info(
    console: Console,
    *,
    provider: str,
    model: str,
    working_directory: Path,
    compaction: str,
    tools: Sequence[str],
) -> None:
    """Render the current runtime configuration before the input loop starts."""
    console.print(
        f"[bold cyan]Python Harness[/bold cyan] · model: {model} · directory: {working_directory}"
    )
    configuration = format_runtime_configuration(
        provider=provider,
        model=model,
        working_directory=working_directory,
        compaction=compaction,
        tools=tools,
    )
    console.print(configuration, style="dim")
    console.print("Type [bold]/help[/bold] to see commands or [bold]/exit[/bold] to quit.")
