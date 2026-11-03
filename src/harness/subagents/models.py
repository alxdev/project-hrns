from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from harness.tools.base import Tool

if TYPE_CHECKING:
    from harness.tools.catalog import ToolCatalog


class SubagentStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SubagentProfile:
    name: str
    description: str
    system_prompt: str
    tools: tuple[Tool, ...] = ()
    skills: tuple[str, ...] = ()
    max_turns: int = 10
    permission_mode: str = "auto_allow"
    requires_tools: bool = False
    tool_artifacts: tuple[str, ...] = ()
    unavailable_artifacts: dict[str, str] = field(default_factory=dict)
    tool_catalog: ToolCatalog | None = field(default=None, repr=False, compare=False)


@dataclass
class SubagentRecord:
    id: str
    profile: str
    prompt: str
    status: SubagentStatus = SubagentStatus.RUNNING
    result: str = ""
    error: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
