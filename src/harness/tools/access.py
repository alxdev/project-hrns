from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from weakref import WeakKeyDictionary

from harness.api.types import ToolDefinition

from .artifacts import ArtifactTool
from .base import Tool
from .handlers import tool_authoring
from .loader import ConfiguredTool

ToolApproval = Callable[[ToolDefinition, dict[str, Any]], Awaitable[bool]]
ErrorReporter = Callable[[str], None]
_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]] = WeakKeyDictionary()


def make_tool_execution_approver(
    approve: ToolApproval,
    tools: Iterable[Tool],
) -> ToolApproval:
    """Let trusted native authoring ask only after its read-only installed checks.

    This wraps only the interactive callback, never a permission policy. The
    handler still denies replacement when dedicated consent is unavailable.
    """
    authoring_definitions = tuple(
        tool.definition
        for tool in tools
        if type(tool) is ConfiguredTool and tool.handler is tool_authoring.execute
    )

    async def ask(tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        if isinstance(arguments, dict) and any(
            tool is definition for definition in authoring_definitions
        ):
            return True
        return await approve(tool, arguments)

    return ask


def _read_profile(path: Path, profile_name: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    profile = profiles.get(profile_name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        return None
    assigned: set[str] = set()
    for field in ("tools", "tool_artifacts"):
        names = profile.get(field, [] if field == "tool_artifacts" else None)
        if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
            raise ValueError(f"profile {profile_name!r} {field} must be an array of strings")
        for name in names:
            if name in assigned:
                raise ValueError(f"profile {profile_name!r} duplicate tool assignment {name!r}")
            assigned.add(name)
    return raw, profile


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def persist_profile_tool_access(
    path: Path,
    profile_name: str,
    tool_name: str,
    approve: ToolApproval,
    tool_catalog: Mapping[str, Tool],
) -> bool:
    """Ask before saving tool access to a configured subagent profile."""
    path = path.resolve()
    tool = tool_catalog.get(tool_name)
    if tool is None:
        return False
    field = "tool_artifacts" if isinstance(tool, ArtifactTool) else "tools"
    other_field = "tools" if field == "tool_artifacts" else "tool_artifacts"
    current = _read_profile(path, profile_name)
    if current is None:
        return False
    _, profile = current
    if tool_name in profile.get(other_field, []):
        raise ValueError(f"tool {tool_name!r} belongs in {field}, not {other_field}")
    if tool_name in profile.get(field, []):
        return True

    definition = ToolDefinition(
        name="save_tool_access",
        description="Persist an approved tool in this subagent profile.",
        input_schema={
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "tool": {"type": "string"},
            },
            "required": ["profile", "tool"],
            "additionalProperties": False,
        },
    )
    if not await approve(definition, {"profile": profile_name, "tool": tool_name}):
        return False
    # Never hold a configuration lock while waiting for user input. Re-read
    # after approval so edits made during that wait survive this update.
    locks = _locks.setdefault(asyncio.get_running_loop(), {})
    lock = locks.setdefault(path, asyncio.Lock())
    async with lock:
        current = _read_profile(path, profile_name)
        if current is None:
            return False
        raw, profile = current
        if tool_name in profile.get(other_field, []):
            raise ValueError(f"tool {tool_name!r} belongs in {field}, not {other_field}")
        tools = profile.get(field, [])
        if tool_name not in tools:
            profile.setdefault("tool_artifacts", [])
            tools = profile[field]
            tools.append(tool_name)
            _atomic_write(path, raw)
    return True


def make_profile_tool_access_persister(
    path: Path,
    profile_name: str,
    approve: ToolApproval,
    tool_catalog: Mapping[str, Tool],
    report_error: ErrorReporter | None = None,
    on_saved: Callable[[str], None] | None = None,
) -> Callable[[str], Awaitable[bool]]:
    """Build the tool-context callback for one configured subagent profile."""

    async def persist(tool_name: str) -> bool:
        try:
            saved = await persist_profile_tool_access(
                path, profile_name, tool_name, approve, tool_catalog
            )
        except (OSError, TypeError, ValueError) as exc:
            if report_error is not None:
                report_error(f"could not save tool access: {exc}")
            return False
        if saved and on_saved is not None:
            on_saved(tool_name)
        return saved

    return persist
