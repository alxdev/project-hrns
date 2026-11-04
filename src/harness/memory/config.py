"""Visible workspace controls for optional experience memory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = False
    write_policy: Literal["inherit", "deny"] = "inherit"
    max_records: int = 200
    max_recall_records: int = 5
    recall_token_budget: int = 2000


def load_memory_config(path: Path) -> MemoryConfig:
    if not path.exists():
        return MemoryConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read memory configuration: {exc}") from exc
    allowed = {
        "enabled",
        "write_policy",
        "max_records",
        "max_recall_records",
        "recall_token_budget",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("memory.json must be an object containing only documented memory controls")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("memory enabled must be boolean")
    policy = raw.get("write_policy", "inherit")
    if not isinstance(policy, str) or policy not in {"inherit", "deny"}:
        raise ValueError("memory write_policy must be inherit or deny")
    limits = {
        "max_records": (200, 1, 10000),
        "max_recall_records": (5, 1, 50),
        "recall_token_budget": (2000, 256, 16000),
    }
    values: dict[str, int] = {}
    for name, (default, low, high) in limits.items():
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"memory {name} must be an integer from {low} to {high}")
        values[name] = value
    return MemoryConfig(enabled, cast(Literal["inherit", "deny"], policy), **values)
