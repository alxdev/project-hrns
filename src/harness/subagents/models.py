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
    system_prompt: str = (
        "You are a focused subagent. Complete the assigned task and return a concise result."
    )
    max_turns: int = 10
    permission_mode: str = "auto_allow"
    reuse: bool = False


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
