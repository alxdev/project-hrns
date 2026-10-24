from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from harness.tools.base import Tool


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
    max_turns: int = 10
    permission_mode: str = "auto_allow"
    reuse: bool = False
    requires_tools: bool = False


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
