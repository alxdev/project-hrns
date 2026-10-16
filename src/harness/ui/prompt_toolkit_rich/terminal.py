"""Compatibility export for the combined prompt-toolkit/Rich UI."""

from . import create_ui
from .input import PromptToolkitInput
from .output import RichOutput

__all__ = ["PromptToolkitInput", "RichOutput", "create_ui"]
