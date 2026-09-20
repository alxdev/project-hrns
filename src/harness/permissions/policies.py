from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from harness.api.types import ToolDefinition


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class PermissionPolicy(Protocol):
    async def decide(self, tool: ToolDefinition, arguments: dict[str, Any]) -> PermissionDecision: ...


class AlwaysAsk:
    def __init__(self, ask):
        self.ask = ask

    async def decide(self, tool: ToolDefinition, arguments: dict[str, Any]) -> PermissionDecision:
        approved = await self.ask(tool, arguments)
        return PermissionDecision.ALLOW if approved else PermissionDecision.DENY

