from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from harness.agent import Agent
from harness.agent.loop import EventSink, SubagentLifecycleEvent
from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import Tool

from .base import Subagent
from .models import SubagentProfile, SubagentRecord, SubagentStatus

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
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active must be at least 1")
        self.factory = factory
        self.profiles = dict(profiles)
        self.max_active = max_active
        self.events = events
        self.tool_loader = tool_loader
        self.report_unavailable = report_unavailable
        self._agents: dict[str, Agent] = {}
        self._session_artifacts: dict[str, set[str]] = {}
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
            for agent in self._session_agents(profile_name):
                if (
                    assigned
                    or isinstance(agent.tools.get(name), ArtifactTool)
                    or (
                        agent is self._agents.get(profile_name)
                        and name in self._session_artifacts.get(profile_name, ())
                    )
                ):
                    agent.tools.unregister(name)
                    agent.tools.register(tool)

    def _session_agents(self, profile_name: str) -> tuple[Agent, ...]:
        agents = {}
        cached = self._agents.get(profile_name)
        if cached is not None:
            agents[id(cached)] = cached
        for name, agent in self._live_agents.values():
            if name == profile_name:
                agents[id(agent)] = agent
        return tuple(agents.values())

    async def delegate(self, profile_name: str, prompt: str) -> SubagentRecord:
        profile = self.profiles.get(profile_name)
        if profile is None:
            available = ", ".join(sorted(self.profiles)) or "none"
            raise ValueError(f"unknown subagent '{profile_name}'; available: {available}")
        return await self._run_profile(profile, prompt)

    async def _run_profile(self, profile: SubagentProfile, prompt: str) -> SubagentRecord:
        if not prompt.strip():
            raise ValueError("subagent prompt cannot be empty")
        if len(self.active()) >= self.max_active:
            raise RuntimeError(f"maximum active subagents reached ({self.max_active})")

        record = SubagentRecord(id=uuid4().hex[:8], profile=profile.name, prompt=prompt)
        self._records.append(record)
        started = time.monotonic()
        await self._emit(SubagentLifecycleEvent(profile.name, record.id, "started"))
        try:
            discovered_tools = await self.tool_loader(profile.name) if self.tool_loader else ()
            # A save may have completed while discovery was awaiting I/O.
            profile = self.profiles[profile.name]
            profile = self._resolve_tools(profile)
            self._discovered_tools[profile.name] = tuple(discovered_tools)
            if profile.requires_tools and not (*profile.tools, *discovered_tools):
                raise RuntimeError(
                    f"subagent '{profile.name}' has no required tools available; "
                    "refusing to run without its configured capabilities"
                )
            effective_profile = replace(profile, tools=(*profile.tools, *discovered_tools))

            if profile.reuse:
                agent = self._agents.get(profile.name)
                if agent is None:
                    agent = self.factory(effective_profile)
                    self._agents[profile.name] = agent
                else:
                    # Manifest changes must replace (or remove) old executable objects,
                    # including session-only grants on a reused agent.
                    if profile.tool_catalog is not None:
                        session_names = self._session_artifacts.setdefault(profile.name, set())
                        session_names.update(
                            definition.name
                            for definition in agent.tools.definitions()
                            if isinstance(agent.tools.get(definition.name), ArtifactTool)
                            and definition.name not in profile.tool_artifacts
                        )
                        artifact_names = session_names | set(profile.tool_artifacts)
                        for name in artifact_names:
                            agent.tools.unregister(name)
                            try:
                                agent.tools.register(profile.tool_catalog[name])
                            except (KeyError, OSError, UnicodeError, ValueError) as exc:
                                if name in session_names:
                                    reason = f"Unavailable session tool artifact {name!r}: {exc}"
                                    if self.report_unavailable is not None:
                                        self.report_unavailable(reason)
                                    prompt += f"\n\n{reason}"
                    for tool in effective_profile.tools:
                        if agent.tools.get(tool.definition.name) is not tool:
                            agent.tools.unregister(tool.definition.name)
                            agent.tools.register(tool)
            else:
                agent = self.factory(effective_profile)

            self._live_agents[record.id] = (profile.name, agent)
            if profile.unavailable_artifacts:
                prompt += "\n\nUnavailable assigned tool artifacts:\n" + "\n".join(
                    profile.unavailable_artifacts.values()
                )
            record.result = await agent.send(prompt)
            record.status = SubagentStatus.COMPLETED
            return record
        except asyncio.CancelledError:
            record.error = "cancelled"
            record.status = SubagentStatus.CANCELLED
            raise
        except Exception as exc:
            record.error = str(exc)
            record.status = SubagentStatus.FAILED
            raise
        finally:
            self._live_agents.pop(record.id, None)
            record.completed_at = datetime.now()
            await self._emit(
                SubagentLifecycleEvent(
                    profile.name,
                    record.id,
                    record.status.value,
                    time.monotonic() - started,
                )
            )

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
        for agent in self._session_agents(profile_name):
            for definition in agent.tools.definitions():
                session_tool = agent.tools.get(definition.name)
                if session_tool is not None:
                    tools.setdefault(definition.name, session_tool)
        return tuple(tools.values())

    def unregister_tools(self, name_prefix: str) -> int:
        removed = 0
        for agent in self._agents.values():
            for definition in agent.tools.definitions():
                if definition.name.startswith(name_prefix):
                    removed += agent.tools.unregister(definition.name) is not None
        return removed

    def describe(
        self,
        extra_tools: Callable[[str], Iterable[Tool]] | None = None,
        leading_agents: Iterable[str] = (),
    ) -> str:
        agent_entries = list(leading_agents)
        if self.profiles:
            for name, profile in sorted(self.profiles.items()):
                tools = list(self.tools_for_profile(name))
                if extra_tools is not None:
                    known = {tool.definition.name for tool in tools}
                    for tool in extra_tools(name):
                        if tool.definition.name not in known:
                            tools.append(tool)
                            known.add(tool.definition.name)
                tool_names = ", ".join(tool.definition.name for tool in tools) or "none"
                agent_entries.append(
                    f"  agent_{name} — {profile.description} — tools: {tool_names}"
                )
                for artifact, reason in profile.unavailable_artifacts.items():
                    agent_entries.append(f"  unavailable artifact {artifact}: {reason}")
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
