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


@dataclass(frozen=True)
class SubagentProfile:
    name: str
    description: str
    tools: tuple[Tool, ...] = ()


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
