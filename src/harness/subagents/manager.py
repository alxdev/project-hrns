from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import uuid4

from harness.agent import Agent

from .models import SubagentProfile, SubagentRecord, SubagentStatus

AgentFactory = Callable[[SubagentProfile], Agent]


class SubagentManager:
    """Creates, reuses, and tracks named subagent instances."""

    def __init__(
        self,
        factory: AgentFactory,
        profiles: Mapping[str, SubagentProfile],
        max_active: int = 4,
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active must be at least 1")
        self.factory = factory
        self.profiles = dict(profiles)
        self.max_active = max_active
        self._agents: dict[str, Agent] = {}
        self._records: list[SubagentRecord] = []

    async def delegate(self, profile_name: str, prompt: str) -> SubagentRecord:
        profile = self.profiles.get(profile_name)
        if profile is None:
            available = ", ".join(sorted(self.profiles)) or "none"
            raise ValueError(f"unknown subagent '{profile_name}'; available: {available}")
        if not prompt.strip():
            raise ValueError("subagent prompt cannot be empty")
        if len(self.active()) >= self.max_active:
            raise RuntimeError(f"maximum active subagents reached ({self.max_active})")

        agent = self._agents.get(profile.name)
        if agent is None:
            agent = self.factory(profile)
            self._agents[profile.name] = agent
        record = SubagentRecord(id=uuid4().hex[:8], profile=profile.name, prompt=prompt)
        self._records.append(record)
        try:
            record.result = await agent.send(prompt)
            record.status = SubagentStatus.COMPLETED
        except Exception as exc:
            record.error = str(exc)
            record.status = SubagentStatus.FAILED
            raise
        finally:
            record.completed_at = datetime.now()
        return record

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
