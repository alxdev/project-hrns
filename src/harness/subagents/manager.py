from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import uuid4

from harness.agent import Agent
from harness.agent.loop import EventSink, SubagentLifecycleEvent

from .base import Subagent
from .models import SubagentProfile, SubagentRecord, SubagentStatus

AgentFactory = Callable[[SubagentProfile], Agent]


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
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active must be at least 1")
        self.factory = factory
        self.profiles = dict(profiles)
        self.max_active = max_active
        self.events = events
        self._agents: dict[str, Agent] = {}
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

        if profile.reuse:
            agent = self._agents.get(profile.name)
            if agent is None:
                agent = self.factory(profile)
                self._agents[profile.name] = agent
        else:
            agent = self.factory(profile)

        record = SubagentRecord(id=uuid4().hex[:8], profile=profile.name, prompt=prompt)
        self._records.append(record)
        started = time.monotonic()
        await self._emit(SubagentLifecycleEvent(profile.name, record.id, "started"))
        try:
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

    def describe(self) -> str:
        lines = ["Subagents"]
        lines.append(f"  profiles: {', '.join(sorted(self.profiles)) or 'none'}")
        lines.append(f"  active: {len(self.active())}/{self.max_active}")
        if self._records:
            for record in self._records[-5:]:
                lines.append(f"  {record.id} · {record.profile} · {record.status.value}")
        else:
            lines.append("  history: none")
        return "\n".join(lines)
