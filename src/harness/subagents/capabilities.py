"""Compact routing metadata; never tool schemas, skill contents, or live grants."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from harness.skills import SkillCatalog
from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import Tool

from .models import SubagentProfile

CONTROLS = frozenset({"list_available_tools", "request_tool", "activate_skill"})


@dataclass(frozen=True)
class Capability:
    name: str
    kind: str
    access: str
    availability: str
    description: str = ""


@dataclass(frozen=True)
class CapabilitySummary:
    role: str
    permission_mode: str
    resources: tuple[Capability, ...]


def build_capabilities(
    profile: SubagentProfile,
    skills: SkillCatalog | None = None,
    discovered_tools: Iterable[Tool] = (),
    mcp_servers: Iterable[tuple[str, str, str]] = (),
) -> CapabilitySummary:
    """Build from configured assignments and cached observations only."""
    assigned = {tool.definition.name: tool for tool in profile.tools}
    artifacts = dict.fromkeys(profile.tool_artifacts)
    artifacts.update(
        (name, None) for name, tool in assigned.items() if isinstance(tool, ArtifactTool)
    )
    resources = [
        Capability(
            name,
            "control" if name in CONTROLS else "native tool",
            "assigned",
            "configured",
            tool.definition.description,
        )
        for name, tool in assigned.items()
        if name not in artifacts
    ]
    catalog = profile.tool_catalog
    known = catalog.known_artifacts() if catalog is not None else {}
    unassigned_access = "requestable" if "request_tool" in assigned else "unassigned"
    for name in dict.fromkeys((*artifacts, *known)):
        observation = known.get(name, assigned.get(name))
        availability = "undiscovered"
        description = ""
        if isinstance(observation, InvalidArtifactError):
            availability = "unavailable (invalid)"
            description = observation.reason
        elif isinstance(observation, KeyError):
            availability = "unavailable (missing)"
        elif isinstance(observation, (OSError, ValueError)):
            availability = "unavailable (load failed)"
            description = str(observation)
        elif observation is not None:
            availability = "last validated"
            description = observation.definition.description
        elif name in profile.unavailable_artifacts:
            availability = "unavailable"
            description = profile.unavailable_artifacts[name]
        resources.append(
            Capability(
                name,
                "artifact",
                "assigned" if name in artifacts else unassigned_access,
                availability,
                description,
            )
        )
    if catalog is not None:
        resources.extend(
            Capability(
                name, "native tool", unassigned_access, "configured", tool.definition.description
            )
            for name, tool in catalog.native_tools.items()
            if name not in assigned
        )
    for name in profile.skills:
        skill = skills.get(name) if skills is not None else None
        resources.append(
            Capability(
                name,
                "skill",
                "assigned",
                "cataloged" if skill else "metadata unavailable",
                skill.description if skill else "",
            )
        )
    for name, scope, status in mcp_servers:
        if status == "not discovered yet":
            status = "undiscovered"
        elif status.startswith(("discovery failed", "discovery cancelled")):
            status = f"unavailable ({status})"
        resources.append(Capability(name, "MCP server", scope, status))
    resources.extend(
        Capability(
            tool.definition.name,
            "MCP tool",
            "assigned",
            "discovered",
            tool.definition.description,
        )
        for tool in discovered_tools
        if tool.definition.name not in assigned
    )
    return CapabilitySummary(profile.description, profile.permission_mode, tuple(resources))


def _brief(text: str, limit: int = 180) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_capabilities(summary: CapabilitySummary) -> str:
    lines = [summary.role]
    shown: dict[str, int] = {}
    omitted: dict[str, int] = {}
    for resource in summary.resources:
        group = (
            "unassigned resources"
            if resource.access in {"requestable", "unassigned"}
            else "discovered MCP tools"
            if resource.kind == "MCP tool"
            else ""
        )
        if group:
            shown[group] = shown.get(group, 0) + 1
            if shown[group] > 12:
                omitted[group] = omitted.get(group, 0) + 1
                continue
        detail = f" — {_brief(resource.description)}" if resource.description else ""
        lines.append(
            f"{resource.kind} {resource.name} "
            f"[{resource.access}; {_brief(resource.availability)}]{detail}"
        )
    lines.extend(f"Additional {group}: {count} (omitted)." for group, count in omitted.items())
    if not summary.resources:
        lines.append("tools: none")
    lines.append(
        f"Permission mode: {summary.permission_mode}. Assignment and requestability do not "
        "guarantee approval or execution. Skills provide guidance, not tool access. "
        "Availability is last known; undiscovered resources require discovery. "
        "Session-only grants are excluded."
    )
    return "\n".join(lines)
