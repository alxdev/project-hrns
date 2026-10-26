from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from harness.api.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from harness.skills import SkillCatalog
    from harness.tools.registry import ToolRegistry

ToolLogSink = Callable[[str, str, str], Awaitable[None]]


@dataclass(frozen=True)
class ToolContext:
    working_directory: Path
    allowed_read_roots: tuple[Path, ...] = ()
    skill_catalog: SkillCatalog | None = None
    allowed_skills: tuple[str, ...] = ()
    tool_registry: ToolRegistry | None = None
    persist_tool_access: Callable[[str], Awaitable[bool]] | None = None
    emit_log: ToolLogSink | None = None
    artifact_root: Path | None = None
    tool_catalog: Mapping[str, Tool] | None = None
    tool_name: str = ""


def resolve_read_path(context: ToolContext, value: str) -> Path:
    requested = Path(value)
    candidate = requested if requested.is_absolute() else context.working_directory / requested
    resolved = candidate.resolve()
    roots = (
        context.working_directory.resolve(),
        *(root.resolve() for root in context.allowed_read_roots),
    )
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError("path must remain inside the working directory or an allowed skill")
    return resolved


def resolve_write_path(context: ToolContext, value: str) -> Path:
    requested = Path(value)
    if requested.is_absolute():
        raise ValueError("write paths must be relative to the working directory")
    resolved = (context.working_directory / requested).resolve()
    root = context.working_directory.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("write path must remain inside the working directory")
    return resolved


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult: ...
