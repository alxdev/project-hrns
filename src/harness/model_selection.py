from __future__ import annotations

from dataclasses import dataclass

MODEL_PICKER_EXIT = "__exit__"


@dataclass(frozen=True)
class ModelOption:
    """TUI-neutral information needed to select a runtime model."""

    key: str
    name: str
    available: bool
    reason: str | None = None
