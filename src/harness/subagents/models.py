from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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
class ToolUsage:
    requested: int = 0
    execution_started: int = 0
    returned_success: int = 0
    returned_error: int = 0
    permission_denied: int = 0


@dataclass
class ResourceUsage:
    observation_status: Literal["not_started", "observed", "partial", "unavailable"] = "not_started"
    tools: dict[str, ToolUsage] = field(default_factory=dict)
    skills_activated: dict[str, int] = field(default_factory=dict)
    tools_added: set[str] = field(default_factory=set)


@dataclass
class SubagentError:
    stage: str
    type: str
    message: str
    tool: str | None = None
    count: int = 1


@dataclass(frozen=True)
class CapabilityEvidence:
    fingerprint: str
    stage: str
    resources: tuple[dict[str, str], ...]
    resources_omitted: int


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
    resource_usage: ResourceUsage = field(default_factory=ResourceUsage)
    errors: list[SubagentError] = field(default_factory=list)
    capabilities: CapabilityEvidence | None = None
    finalized: bool = False
    working_directory: Path | None = None
