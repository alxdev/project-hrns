from .terminal import TerminalUI

__all__ = ["TerminalUI", "create_ui"]


def create_ui(history_file=None):
    ui = TerminalUI()
    return ui, ui
