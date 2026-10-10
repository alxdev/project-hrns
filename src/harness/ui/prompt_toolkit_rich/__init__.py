from .input import PromptToolkitInput
from .output import RichOutput

TUI_NAME = "prompt_toolkit + rich"

__all__ = [
    "PromptToolkitInput",
    "RichOutput",
    "TUI_NAME",
]


def create_ui(history_file=None):
    return PromptToolkitInput(history_file), RichOutput()
