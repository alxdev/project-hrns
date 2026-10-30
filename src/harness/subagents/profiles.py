from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import Tool
from harness.tools.catalog import ToolCatalog

from .models import SubagentProfile

PERMISSION_MODES = frozenset({"auto_allow", "inherit", "ask", "deny"})


def load_profiles(
    path: Path, tools: Iterable[Tool] | Mapping[str, Tool]
) -> dict[str, SubagentProfile]:
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

    tool_by_name = (
        tools.native_tools
        if isinstance(tools, ToolCatalog)
        else tools
        if isinstance(tools, Mapping)
        else {tool.definition.name: tool for tool in tools}
    )
    profiles: dict[str, SubagentProfile] = {}
    for name, config in raw["profiles"].items():
        profiles[name] = _build_profile(
            path, name, config, tool_by_name, tools if isinstance(tools, ToolCatalog) else None
        )
    return profiles


def _build_profile(
    path: Path,
    name: object,
    config: object,
    tool_by_name: Mapping[str, Tool],
    tool_catalog: ToolCatalog | None = None,
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

    selected_tools: list[Tool] = []
    unavailable_artifacts: dict[str, str] = {}
    assigned: set[str] = set()
    fields = {"tools": config.get("tools"), "tool_artifacts": config.get("tool_artifacts", [])}
    for field, configured in fields.items():
        if not isinstance(configured, list) or not all(
            isinstance(item, str) for item in configured
        ):
            raise ValueError(f"{path} profile {name!r} {field} must be an array of strings")
        for tool_name in configured:
            if tool_name in assigned:
                raise ValueError(f"{path} profile {name!r} duplicate tool assignment {tool_name!r}")
            assigned.add(tool_name)
    for field, configured in fields.items():
        for tool_name in configured:
            if tool_catalog is not None and field == "tool_artifacts":
                if tool_name in tool_catalog.native_tools:
                    raise ValueError(
                        f"{path} profile {name!r} tool {tool_name!r} belongs in tools, "
                        "not tool_artifacts"
                    )
                # Retain assignment intent without touching the artifact filesystem.
                continue
            try:
                tool = tool_by_name.get(tool_name)
            except InvalidArtifactError as exc:
                if field != "tool_artifacts":
                    raise ValueError(
                        f"{path} profile {name!r} tool {tool_name!r} belongs in "
                        "tool_artifacts, not tools"
                    ) from exc
                unavailable_artifacts[tool_name] = str(exc)
                continue
            except OSError as exc:
                raise ValueError(
                    f"{path} profile {name!r} cannot load tool {tool_name!r}: {exc}"
                ) from exc
            if tool is None:
                raise ValueError(f"{path} profile {name!r} references unknown tool {tool_name!r}")
            expected = "tool_artifacts" if isinstance(tool, ArtifactTool) else "tools"
            if field != expected:
                raise ValueError(
                    f"{path} profile {name!r} tool {tool_name!r} belongs in {expected}, not {field}"
                )
            selected_tools.append(tool)

    configured_skills = config.get("skills", [])
    if not isinstance(configured_skills, list) or not all(
        isinstance(skill_name, str) and skill_name.strip() for skill_name in configured_skills
    ):
        raise ValueError(f"{path} profile {name!r} skills must be an array of strings")
    skill_names = tuple(configured_skills)
    selected_names = {tool.definition.name for tool in selected_tools}
    for control_tool in ("request_tool", "list_available_tools", "activate_skill"):
        if (
            control_tool in tool_by_name
            and (control_tool != "activate_skill" or skill_names)
            and control_tool not in selected_names
        ):
            selected_tools.append(tool_by_name[control_tool])

    max_turns = config.get("max_turns", 10)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise ValueError(f"{path} profile {name!r} max_turns must be a positive integer")
    requires_tools = _optional_bool(path, name, config, "requires_tools", False)
    return SubagentProfile(
        name=name,
        description=description,
        system_prompt=system_prompt,
        tools=tuple(selected_tools),
        skills=skill_names,
        max_turns=max_turns,
        permission_mode=permission_mode,
        requires_tools=requires_tools,
        tool_artifacts=tuple(fields["tool_artifacts"]),
        unavailable_artifacts=unavailable_artifacts,
        tool_catalog=tool_catalog,
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
