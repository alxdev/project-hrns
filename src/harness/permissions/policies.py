from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

from harness.api.types import ToolDefinition


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class PermissionPolicy(Protocol):
    async def decide(
        self, tool: ToolDefinition, arguments: dict[str, Any]
    ) -> PermissionDecision: ...


class AlwaysAsk:
    def __init__(self, ask: Callable[[ToolDefinition, dict[str, Any]], Awaitable[bool]]) -> None:
        self.ask = ask

    async def decide(self, tool: ToolDefinition, arguments: dict[str, Any]) -> PermissionDecision:
        approved = await self.ask(tool, arguments)
        return PermissionDecision.ALLOW if approved else PermissionDecision.DENY


class AlwaysDeny:
    async def decide(self, tool: ToolDefinition, arguments: dict[str, Any]) -> PermissionDecision:
        return PermissionDecision.DENY


class AlwaysAllow:
    async def decide(self, tool: ToolDefinition, arguments: dict[str, Any]) -> PermissionDecision:
        return PermissionDecision.ALLOW


class RoutedPermissionPolicy:
    """Apply an alternate policy to tools whose names start with a prefix."""

    def __init__(
        self,
        default: PermissionPolicy,
        overrides: dict[str, PermissionPolicy],
    ) -> None:
        self.default = default
        self.overrides = overrides

    async def decide(self, tool: ToolDefinition, arguments: dict[str, Any]) -> PermissionDecision:
        policy = next(
            (policy for prefix, policy in self.overrides.items() if tool.name.startswith(prefix)),
            self.default,
        )
        return await policy.decide(tool, arguments)
