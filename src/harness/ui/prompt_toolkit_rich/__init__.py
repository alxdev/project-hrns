from .input import PromptToolkitInput
from .output import RichOutput

__all__ = [
    "PromptToolkitInput",
    "RichOutput",
]


def create_ui(history_file=None):
    return PromptToolkitInput(history_file), RichOutput()
