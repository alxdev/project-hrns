from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from harness.agent import Agent
from harness.agent.loop import EventSink, SubagentLifecycleEvent
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
        record = await self._manager._run_profile(self._profile, task)
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
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active must be at least 1")
        self.factory = factory
        self.profiles = dict(profiles)
        self.max_active = max_active
        self.events = events
        self.tool_loader = tool_loader
        self._agents: dict[str, Agent] = {}
        self._discovered_tools: dict[str, tuple[Tool, ...]] = {}
        self._records: list[SubagentRecord] = []

    def get(self, profile_name: str) -> Subagent:
        profile = self.profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"unknown subagent: {profile_name}")
        return ProfileSubagent(self, profile)

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
                    for tool in discovered_tools:
                        if not any(
                            definition.name == tool.definition.name
                            for definition in agent.tools.definitions()
                        ):
                            agent.tools.register(tool)
            else:
                agent = self.factory(effective_profile)

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
            record.completed_at = datetime.now()
            await self._emit(
                SubagentLifecycleEvent(
                    profile.name,
                    record.id,
                    record.status.value,
                    time.monotonic() - started,
                )
            )

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
