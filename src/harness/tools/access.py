from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from weakref import WeakKeyDictionary

from harness.api.types import ToolDefinition

ToolApproval = Callable[[ToolDefinition, dict[str, Any]], Awaitable[bool]]
ErrorReporter = Callable[[str], None]
_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]] = WeakKeyDictionary()


def _read_profile(path: Path, profile_name: str) -> tuple[dict[str, Any], list[str]] | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    profile = profiles.get(profile_name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        return None
    tools = profile.get("tools")
    if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
        return None
    return raw, tools


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
) -> bool:
    """Ask before saving tool access to a configured subagent profile."""
    path = path.resolve()
    current = _read_profile(path, profile_name)
    if current is None:
        return False
    _, tools = current
    if tool_name in tools:
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
        raw, tools = current
        if tool_name not in tools:
            tools.append(tool_name)
            _atomic_write(path, raw)
    return True


def make_profile_tool_access_persister(
    path: Path,
    profile_name: str,
    approve: ToolApproval,
    report_error: ErrorReporter | None = None,
) -> Callable[[str], Awaitable[bool]]:
    """Build the tool-context callback for one configured subagent profile."""

    async def persist(tool_name: str) -> bool:
        try:
            return await persist_profile_tool_access(path, profile_name, tool_name, approve)
        except (OSError, TypeError, ValueError) as exc:
            if report_error is not None:
                report_error(f"could not save tool access: {exc}")
            return False

    return persist
