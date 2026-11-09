"""Declarative capability assignments for the root planner/router."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from harness.skills import SkillCatalog
from harness.tools.base import Tool
from harness.tools.handlers.activate_skill import execute as activate_skill
from harness.tools.loader import ConfiguredTool

MEMORY_TOOLS = frozenset({"recall_experience", "record_experience"})


@dataclass(frozen=True)
class OrchestratorConfig:
    tools: tuple[str, ...]
    skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ("*",)
    max_turns: int = 20
    explicit: bool = False
    inactive_tools: tuple[str, ...] = ()

    @property
    def active_tools(self) -> tuple[str, ...]:
        return tuple(name for name in self.tools if name not in self.inactive_tools)

    @property
    def mode(self) -> str:
        return (
            "orchestrator.json" if self.explicit else "legacy defaults (orchestrator.json absent)"
        )

    def admits_mcp_server(self, name: str) -> bool:
        return "*" in self.mcp_servers or name in self.mcp_servers

    def describe(self) -> str:
        inactive = ", ".join(self.inactive_tools) or "none"
        return (
            f"root configuration: {self.mode}; skills: {', '.join(self.skills) or 'none'}; "
            f"MCP servers: {', '.join(self.mcp_servers) or 'none'}; "
            f"inactive tools (memory disabled): {inactive}; max_turns: {self.max_turns}"
        )


def load_orchestrator(
    path: Path,
    *,
    native_tools: Iterable[Tool],
    skills: SkillCatalog,
    memory_enabled: bool,
) -> OrchestratorConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return OrchestratorConfig(
            tools=("delegate_subagent",) + (tuple(sorted(MEMORY_TOOLS)) if memory_enabled else ())
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("orchestrator.json must contain an object")
    unknown = raw.keys() - {"tools", "skills", "mcp_servers", "max_turns"}
    if unknown:
        raise ValueError(f"orchestrator.json unknown fields: {', '.join(sorted(unknown))}")
    selected = _names(raw, "tools")
    assigned_skills = _names(raw, "skills")
    servers = _names(raw, "mcp_servers")
    if "*" in servers and servers != ("*",):
        raise ValueError("orchestrator.json mcp_servers '*' must be used alone")
    if any(name != "*" and not re.fullmatch(r"[a-z0-9_-]+", name) for name in servers):
        raise ValueError("orchestrator.json mcp_servers must use normalized server names or '*'")
    turns = raw.get("max_turns", 20)
    if type(turns) is not int or turns < 1:
        raise ValueError("orchestrator.json max_turns must be a positive integer")
    obsolete = [
        name for name in selected if name.startswith("delegate_") and name != "delegate_subagent"
    ]
    if obsolete:
        raise ValueError(
            f"orchestrator.json tools contains obsolete per-profile tools: {', '.join(obsolete)}; "
            "replace them with delegate_subagent and pass the profile name in each call"
        )
    allowed = MEMORY_TOOLS | {"activate_skill", "delegate_subagent"}
    invalid = set(selected) - allowed
    if invalid:
        raise ValueError(
            f"orchestrator.json tools contains unknown root tools: {', '.join(sorted(invalid))}"
        )
    if "delegate_subagent" not in selected:
        raise ValueError("orchestrator.json tools requires delegate_subagent")
    missing_skills = [name for name in assigned_skills if skills.get(name) is None]
    if missing_skills:
        raise ValueError(
            f"orchestrator.json skills contains unknown skills: {', '.join(missing_skills)}"
        )
    if assigned_skills and "activate_skill" not in selected:
        raise ValueError("orchestrator.json skills requires activate_skill in tools")
    if "activate_skill" in selected:
        activation = next(
            (tool for tool in native_tools if tool.definition.name == "activate_skill"), None
        )
        if not isinstance(activation, ConfiguredTool) or activation.handler is not activate_skill:
            raise ValueError(
                "orchestrator.json tools activate_skill requires the activate_skill handler "
                "in tools.json"
            )
    return OrchestratorConfig(
        tools=selected,
        skills=assigned_skills,
        mcp_servers=servers,
        max_turns=turns,
        explicit=True,
        inactive_tools=tuple(
            name for name in selected if name in MEMORY_TOOLS and not memory_enabled
        ),
    )


def _names(raw: dict[str, object], field: str) -> tuple[str, ...]:
    value = raw.get(field)
    if not isinstance(value, list) or any(
        not isinstance(name, str) or not name or name != name.strip() for name in value
    ):
        raise ValueError(f"orchestrator.json {field} must be an array of non-empty trimmed strings")
    if len(value) != len(set(value)):
        raise ValueError(f"orchestrator.json {field} contains duplicate names")
    return tuple(value)
