from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from harness.tools.base import Tool

from .models import SubagentProfile

PERMISSION_MODES = frozenset({"auto_allow", "inherit", "ask", "deny"})


def load_profiles(path: Path, tools: Iterable[Tool]) -> dict[str, SubagentProfile]:
    """Load user-configured subagent profiles from JSON.

    A missing file is equivalent to an empty profile set. The file is
    authoritative when present; no profiles are defined in Python as a
    fallback.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc.msg}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
        raise ValueError(f"{path} must contain a 'profiles' object")

    tool_by_name = {tool.definition.name: tool for tool in tools}
    profiles: dict[str, SubagentProfile] = {}
    for name, config in raw["profiles"].items():
        profiles[name] = _build_profile(path, name, config, tool_by_name)
    return profiles


def _build_profile(
    path: Path,
    name: object,
    config: object,
    tool_by_name: dict[str, Tool],
) -> SubagentProfile:
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError(f"{path} profile names must be non-empty trimmed strings")
    if not isinstance(config, dict):
        raise ValueError(f"{path} profile {name!r} must be an object")

    description = _required_string(path, name, config, "description")
    system_prompt = _required_string(path, name, config, "system_prompt")
    permission_mode = _required_string(path, name, config, "permission_mode")
    if permission_mode not in PERMISSION_MODES:
        allowed = ", ".join(sorted(PERMISSION_MODES))
        raise ValueError(f"{path} profile {name!r} permission_mode must be one of: {allowed}")

    configured_tools = config.get("tools")
    if not isinstance(configured_tools, list) or not all(
        isinstance(tool_name, str) for tool_name in configured_tools
    ):
        raise ValueError(f"{path} profile {name!r} tools must be an array of strings")
    selected_tools: list[Tool] = []
    for tool_name in configured_tools:
        tool = tool_by_name.get(tool_name)
        if tool is None:
            raise ValueError(f"{path} profile {name!r} references unknown tool {tool_name!r}")
        selected_tools.append(tool)

    max_turns = config.get("max_turns", 10)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise ValueError(f"{path} profile {name!r} max_turns must be a positive integer")
    reuse = _optional_bool(path, name, config, "reuse", False)
    requires_tools = _optional_bool(path, name, config, "requires_tools", False)
    return SubagentProfile(
        name=name,
        description=description,
        system_prompt=system_prompt,
        tools=tuple(selected_tools),
        max_turns=max_turns,
        permission_mode=permission_mode,
        reuse=reuse,
        requires_tools=requires_tools,
    )


def _required_string(path: Path, name: str, config: dict[str, Any], field: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} profile {name!r} requires a non-empty string {field}")
    return value


def _optional_bool(
    path: Path, name: str, config: dict[str, Any], field: str, default: bool
) -> bool:
    value = config.get(field, default)
    if not isinstance(value, bool):
        raise ValueError(f"{path} profile {name!r} {field} must be a boolean")
    return value
