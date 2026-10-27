from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISLNK, S_ISREG
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
    approve_artifact_replacement: (
        Callable[[ToolDefinition, dict[str, Any]], Awaitable[bool]] | None
    ) = None
    on_artifact_replaced: Callable[[Tool], None] | None = None


def resolve_read_path(context: ToolContext, value: str, *, skill: str | None = None) -> Path:
    if not isinstance(value, str):
        raise ValueError("path must be a string")
    requested = Path(value)
    roots = (
        context.working_directory.resolve(),
        *(root.resolve() for root in context.allowed_read_roots),
    )
    if skill is not None:
        if not isinstance(skill, str) or not skill:
            raise ValueError("skill must be a non-empty string")
        if skill not in context.allowed_skills:
            raise ValueError(f"skill {skill!r} is not allowed for this subagent")
        selected = context.skill_catalog.get(skill) if context.skill_catalog else None
        if selected is None:
            raise ValueError(f"unknown skill {skill!r}")
        if requested.is_absolute():
            raise ValueError("paths with an explicit skill must be relative")
        root = selected.root.resolve()
        resolved = (root / requested).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("path must remain inside the selected skill")
        if not any(resolved.is_relative_to(allowed_root) for allowed_root in roots):
            raise ValueError("path must remain inside the working directory or an allowed skill")
        return resolved

    candidate = requested if requested.is_absolute() else context.working_directory / requested
    resolved = candidate.resolve()
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError("path must remain inside the working directory or an allowed skill")
    if requested.is_absolute():
        return resolved
    try:
        resolved.stat()
    except FileNotFoundError:
        # A stale workspace link (including an ancestor) still shadows skill resources.
        for ancestor in (candidate, *candidate.parents):
            try:
                mode = ancestor.lstat().st_mode
            except FileNotFoundError:
                continue
            if S_ISLNK(mode):
                try:
                    ancestor.stat()
                except FileNotFoundError:
                    return resolved
            break
    else:
        return resolved

    # Only a missing workspace path permits fallback; other filesystem errors propagate.
    matches: set[Path] = set()
    for allowed_root in context.allowed_read_roots:
        root = allowed_root.resolve()
        resource = (root / requested).resolve()
        if not resource.is_relative_to(root):
            continue
        try:
            if not S_ISREG(resource.stat().st_mode):
                continue
            with resource.open("rb"):
                pass
        except FileNotFoundError:
            continue
        matches.add(resource)
    if len(matches) > 1:
        paths = ", ".join(str(path) for path in sorted(matches))
        raise ValueError(
            f"ambiguous skill resource {value!r}: {paths}; "
            "use an explicit skill name or an authorized absolute path"
        )
    return next(iter(matches)) if matches else resolved


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
