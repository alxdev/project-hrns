from __future__ import annotations

from pathlib import Path
from typing import Sequence


def format_runtime_configuration(
    *,
    provider: str,
    model: str,
    working_directory: Path,
    compaction: str,
    tools: Sequence[str],
    tui: str = "not applicable",
) -> str:
    return "\n".join(
        [
            "Runtime configuration",
            f"  provider: {provider}",
            f"  model: {model}",
            f"  directory: {working_directory}",
            f"  compaction: {compaction}",
            f"  TUI: {tui}",
            f"  tools: {', '.join(tools) if tools else 'none'}",
            "  approval: required before every tool call",
        ]
    )


def format_startup_message(
    *,
    provider: str,
    model: str,
    working_directory: Path,
    compaction: str,
    tools: Sequence[str],
    tui: str = "not applicable",
) -> str:
    return "\n".join(
        [
            f"Python Harness · model: {model} · directory: {working_directory}",
            format_runtime_configuration(
                provider=provider,
                model=model,
                working_directory=working_directory,
                compaction=compaction,
                tools=tools,
                tui=tui,
            ),
            "Type /help to see commands or /exit to quit.",
            "Shortcuts: Ctrl-C cancel input · Ctrl-D exit.",
        ]
    )
