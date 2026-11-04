from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from harness.agent import Agent
from harness.agent.loop import EventSink, SubagentLifecycleEvent
from harness.request_context import render_handoff
from harness.skills import SkillCatalog
from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import Tool

from .base import Subagent
from .capabilities import CapabilitySummary, build_capabilities, format_capabilities
from .models import SubagentProfile, SubagentRecord, SubagentStatus
from .observations import InvocationObserver
from .outcomes import capability_evidence, runtime_error

if TYPE_CHECKING:
    from harness.mcp import MCPToolManager

AgentFactory = Callable[[SubagentProfile], Agent]
ToolLoader = Callable[[str], Awaitable[Iterable[Tool]]]


class ProfileSubagent:
    def __init__(self, manager: SubagentManager, profile: SubagentProfile) -> None:
        self._manager = manager
        self._profile = profile

    @property
    def name(self) -> str:
        return self._profile.name

    @property
    def description(self) -> str:
        return self._profile.description

    async def run(self, task: str) -> str:
        record = await self._manager.delegate(self.name, task)
        return record.result


class SubagentManager:
    """Registry, factory, and in-memory tracker for configured subagents."""

    def __init__(
        self,
        factory: AgentFactory,
        profiles: Mapping[str, SubagentProfile],
        max_active: int = 4,
        events: EventSink | None = None,
        tool_loader: ToolLoader | None = None,
        report_unavailable: Callable[[str], None] | None = None,
        skill_catalog: SkillCatalog | None = None,
        mcp: MCPToolManager | None = None,
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active must be at least 1")
        self.factory = factory
        self.profiles = dict(profiles)
        self.max_active = max_active
        self.events = events
        self.tool_loader = tool_loader
        self.report_unavailable = report_unavailable
        self.skill_catalog = skill_catalog
        self.mcp = mcp
        self._live_agents: dict[str, tuple[str, Agent]] = {}
        self._discovered_tools: dict[str, tuple[Tool, ...]] = {}
        self._records: list[SubagentRecord] = []

    def get(self, profile_name: str) -> Subagent:
        profile = self.profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"unknown subagent: {profile_name}")
        return ProfileSubagent(self, profile)

    def grant_tool_access(self, profile_name: str, tool: Tool) -> None:
        """Merge a successfully persisted grant into this manager's current profile."""
        profile = self.profiles[profile_name]
        name = tool.definition.name
        selected = tuple(tool if item.definition.name == name else item for item in profile.tools)
        if not any(item.definition.name == name for item in selected):
            selected = (*selected, tool)
        artifacts = profile.tool_artifacts
        if isinstance(tool, ArtifactTool) and name not in artifacts:
            artifacts = (*artifacts, name)
        self.profiles[profile_name] = replace(
            profile,
            tools=selected,
            tool_artifacts=artifacts,
            unavailable_artifacts={
                key: value for key, value in profile.unavailable_artifacts.items() if key != name
            },
        )

    def refresh_artifact_access(self, tool: Tool) -> None:
        """Refresh an installed replacement only where access already exists.

        Session-only grants remain session-only; they never become profile grants.
        This synchronous update cannot interleave registry removal and insertion.
        """
        name = tool.definition.name
        for profile_name, profile in tuple(self.profiles.items()):
            assigned = name in profile.tool_artifacts or any(
                isinstance(item, ArtifactTool) and item.definition.name == name
                for item in profile.tools
            )
            if assigned:
                self.grant_tool_access(profile_name, tool)
            for agent in self._active_agents(profile_name):
                if assigned or isinstance(agent.tools.get(name), ArtifactTool):
                    agent.tools.unregister(name)
                    agent.tools.register(tool)

    def _active_agents(self, profile_name: str) -> tuple[Agent, ...]:
        return tuple(agent for name, agent in self._live_agents.values() if name == profile_name)

    async def delegate(
        self, profile_name: str, prompt: str, *, capture_errors: bool = False
    ) -> SubagentRecord:
        profile = self.profiles.get(profile_name)
        if profile is None:
            available = ", ".join(sorted(self.profiles)) or "none"
            raise ValueError(f"unknown subagent '{profile_name}'; available: {available}")
        return await self._run_profile(profile, prompt, capture_errors=capture_errors)

    async def _run_profile(
        self,
        profile: SubagentProfile,
        prompt: str,
        *,
        capture_errors: bool = False,
    ) -> SubagentRecord:
        if not prompt.strip():
            raise ValueError("subagent prompt cannot be empty")
        if len(self.active()) >= self.max_active:
            raise RuntimeError(f"maximum active subagents reached ({self.max_active})")

        record = SubagentRecord(id=uuid4().hex[:8], profile=profile.name, prompt=prompt)
        self._records.append(record)
        started = time.monotonic()
        stage = "initialization"
        observer: InvocationObserver | None = None
        agent: Agent | None = None
        try:
            record.capabilities = capability_evidence(self.capabilities(profile.name), "configured")
            stage = "lifecycle_start"
            await self._emit(SubagentLifecycleEvent(profile.name, record.id, "started"))
            notices: list[str] = []
            stage = "discovery"
            discovered_tools = await self.tool_loader(profile.name) if self.tool_loader else ()
            # A save may have completed while discovery was awaiting I/O.
            profile = self.profiles[profile.name]
            stage = "tool_resolution"
            profile = self._resolve_tools(profile)
            self._discovered_tools[profile.name] = tuple(discovered_tools)
            if profile.requires_tools and not (*profile.tools, *discovered_tools):
                raise RuntimeError(
                    f"subagent '{profile.name}' has no required tools available; "
                    "refusing to run without its configured capabilities"
                )
            effective_profile = replace(profile, tools=(*profile.tools, *discovered_tools))

            stage = "factory"
            agent = self.factory(effective_profile)
            record.capabilities = capability_evidence(
                self.capabilities(profile.name), "before_child_run"
            )
            if isinstance(agent, Agent):
                observer = InvocationObserver(record, agent.events, agent.tools)
                agent.events = observer
            else:
                record.resource_usage.observation_status = "unavailable"

            self._live_agents[record.id] = (profile.name, agent)
            if profile.unavailable_artifacts:
                notices.extend(profile.unavailable_artifacts.values())
            stage = "child"
            record.result = await agent.send(render_handoff(prompt, notices))
            record.status = SubagentStatus.COMPLETED
        except asyncio.CancelledError as exc:
            record.error = "cancelled"
            record.status = SubagentStatus.CANCELLED
            runtime_error(record, stage, exc)
            raise
        except Exception as exc:
            record.error = str(exc)
            record.status = SubagentStatus.FAILED
            runtime_error(record, stage, exc)
            if not capture_errors:
                raise
        finally:
            if observer is not None and agent is not None:
                try:
                    observer.observe_tools()
                except Exception as exc:
                    record.resource_usage.observation_status = "partial"
                    runtime_error(record, "observation", exc)
                finally:
                    agent.events = observer.downstream
            self._live_agents.pop(record.id, None)
            record.completed_at = datetime.now()
            try:
                await self._emit(
                    SubagentLifecycleEvent(
                        profile.name,
                        record.id,
                        record.status.value,
                        time.monotonic() - started,
                    )
                )
            except asyncio.CancelledError as exc:
                record.error = "cancelled"
                record.status = SubagentStatus.CANCELLED
                runtime_error(record, "lifecycle_end", exc)
                raise
            except Exception as exc:
                runtime_error(record, "lifecycle_end", exc)
                if record.status is not SubagentStatus.CANCELLED:
                    record.error = str(exc)
                    record.status = SubagentStatus.FAILED
                    if not capture_errors:
                        raise
        return record

    def _resolve_tools(self, profile: SubagentProfile) -> SubagentProfile:
        catalog = profile.tool_catalog
        if catalog is None:
            return profile
        tools = [
            catalog[tool.definition.name] if tool.definition.name in catalog.native_tools else tool
            for tool in profile.tools
            if not isinstance(tool, ArtifactTool)
        ]
        unavailable: dict[str, str] = {}
        for name in profile.tool_artifacts:
            try:
                tools.append(catalog[name])
            except KeyError:
                unavailable[name] = f"tool artifact {name!r}: missing artifact directory"
            except InvalidArtifactError as exc:
                unavailable[name] = str(exc)
            except (OSError, UnicodeError, ValueError) as exc:
                unavailable[name] = f"cannot load tool artifact {name!r}: {exc}"
        profile = replace(profile, tools=tuple(tools), unavailable_artifacts=unavailable)
        self.profiles[profile.name] = profile
        if self.report_unavailable is not None:
            for reason in unavailable.values():
                self.report_unavailable(f"Profile {profile.name!r}: {reason}")
        return profile

    async def _emit(self, event: object) -> None:
        if self.events is not None:
            await self.events.emit(event)

    def active(self) -> list[SubagentRecord]:
        return [record for record in self._records if record.status is SubagentStatus.RUNNING]

    def records(self) -> list[SubagentRecord]:
        return list(self._records)

    def tools_for_profile(self, profile_name: str) -> tuple[Tool, ...]:
        profile = self.profiles.get(profile_name)
        if profile is None:
            return ()
        tools: dict[str, Tool] = {}
        for tool in (*profile.tools, *self._discovered_tools.get(profile_name, ())):
            tools.setdefault(tool.definition.name, tool)
        for agent in self._active_agents(profile_name):
            for definition in agent.tools.definitions():
                session_tool = agent.tools.get(definition.name)
                if session_tool is not None:
                    tools.setdefault(definition.name, session_tool)
        return tuple(tools.values())

    def unregister_tools(self, name_prefix: str) -> int:
        removed = 0
        for profile_name, tools in self._discovered_tools.items():
            self._discovered_tools[profile_name] = tuple(
                tool for tool in tools if not tool.definition.name.startswith(name_prefix)
            )
        for _, agent in self._live_agents.values():
            for definition in agent.tools.definitions():
                if definition.name.startswith(name_prefix):
                    removed += agent.tools.unregister(definition.name) is not None
        return removed

    def capabilities(
        self,
        profile_name: str,
        *,
        mcp: MCPToolManager | None = None,
    ) -> CapabilitySummary:
        """Metadata for future delegations, without discovery or session grants."""
        source = mcp if mcp is not None else self.mcp
        servers: list[tuple[str, str, str]] = []
        discovered = self._discovered_tools.get(profile_name, ())
        if source is not None:
            # The MCP cache filters current scope assignments, unlike past delegations.
            discovered = source.cached_tools_for(profile_name)
            servers = source.discovery_details(profile_name)
        return build_capabilities(
            self.profiles[profile_name], self.skill_catalog, discovered, servers
        )

    def capability_description(self, profile_name: str) -> str:
        return format_capabilities(self.capabilities(profile_name))

    def describe(
        self,
        leading_agents: Iterable[str] = (),
        *,
        mcp: MCPToolManager | None = None,
    ) -> str:
        agent_entries = list(leading_agents)
        if self.profiles:
            for name in sorted(self.profiles):
                summary = format_capabilities(self.capabilities(name, mcp=mcp))
                agent_entries.append(f"  agent_{name} — " + summary.replace("\n", "\n    "))
        else:
            agent_entries.append("  profiles: none")
        sections = ["Agents", *agent_entries, f"active: {len(self.active())}/{self.max_active}"]
        if self._records:
            history = [
                f"  {record.id} · {record.profile} · {record.status.value}"
                for record in self._records[-5:]
            ]
            sections.append("Recent delegations\n" + "\n\n".join(history))
        else:
            sections.append("Recent delegations\n  none")
        return "\n\n".join(sections)
