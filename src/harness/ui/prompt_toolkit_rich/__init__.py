from pathlib import Path

from .input import PromptToolkitInput
from .output import RichOutput

TUI_NAME = "prompt_toolkit + rich"

__all__ = [
    "PromptToolkitInput",
    "RichOutput",
    "TUI_NAME",
]


def create_ui(history_file: Path | None = None) -> tuple[PromptToolkitInput, RichOutput]:
    if history_file is None:
        history_file = Path("~/.python_harness_history").expanduser()
    return PromptToolkitInput(history_file), RichOutput()
