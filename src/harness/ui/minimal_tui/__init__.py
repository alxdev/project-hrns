from .terminal import TerminalUI

TUI_NAME = "minimal terminal"

__all__ = ["TerminalUI", "TUI_NAME", "create_ui"]


def create_ui(history_file=None):
    ui = TerminalUI()
    return ui, ui
