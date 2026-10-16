from pathlib import Path

from .terminal import TerminalUI

TUI_NAME = "minimal terminal"

__all__ = ["TerminalUI", "TUI_NAME", "create_ui"]


def create_ui(history_file: Path | None = None) -> tuple[TerminalUI, TerminalUI]:
    ui = TerminalUI()
    return ui, ui
