from __future__ import annotations

import asyncio
import importlib
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

from harness.agent.loop import EventSink, MCPDiscoveryEvent
from harness.api.types import ToolDefinition
from harness.tools.base import Tool

from .config import MCPConfigStore, MCPServerConfig
from .tools import MCPTool


class MCPToolManager:
    """Lazily discovers MCP tools and keeps each connection for the session."""

    def __init__(
        self,
        store: MCPConfigStore,
        events: EventSink,
    ) -> None:
        self.store = store
        self.events = events
        self._load_tasks: dict[tuple[str, str], asyncio.Task[tuple[Tool, ...]]] = {}
        self._clients: list[Any] = []
        self._client_factory: Any = None
        self._tools_by_scope: dict[str, tuple[Tool, ...]] = {}

    def registered_servers(self) -> list[MCPServerConfig]:
        return self.store.load()

    def register(self, server: MCPServerConfig) -> None:
        self.store.add(server)
        self._tools_by_scope.clear()

    def unregister(self, name: str) -> int:
        removed = self.store.remove(name)
        self._tools_by_scope.clear()
        return removed

    async def tools_for(self, profile: str) -> tuple[Tool, ...]:
        cached = self._tools_by_scope.get(profile)
        if cached is not None:
            return cached
        try:
            servers = [server for server in self.store.load() if server.applies_to(profile)]
        except ValueError as exc:
            await self.events.emit(MCPDiscoveryEvent(profile, "mcp.json", "started"))
            await self.events.emit(MCPDiscoveryEvent(profile, "mcp.json", "failed", error=str(exc)))
            self._tools_by_scope[profile] = ()
            return ()
        results = await asyncio.gather(*(self._load_once(server, profile) for server in servers))
        unique: dict[str, Tool] = {}
        for group in results:
            for tool in group:
                unique.setdefault(tool.definition.name, tool)
        tools = tuple(unique.values())
        self._tools_by_scope[profile] = tools
        return tools

    async def _load_once(self, server: MCPServerConfig, profile: str) -> tuple[Tool, ...]:
        key = (server.name, server.url)
        task = self._load_tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._discover(server, profile))
            self._load_tasks[key] = task
        return await task

    async def _discover(self, server: MCPServerConfig, profile: str) -> tuple[Tool, ...]:
        label = self._display_name(server.name)
        await self.events.emit(MCPDiscoveryEvent(profile, label, "started"))
        try:
            client = await self._connect(server.url)
            response = await client.list_tools()
            remote_tools = getattr(response, "tools", ())
            adapted: list[Tool] = []
            seen: set[str] = set()
            for remote in remote_tools:
                remote_name = getattr(remote, "name", None)
                if not isinstance(remote_name, str) or not remote_name:
                    continue
                tool_name = f"mcp_{server.name}_{remote_name}"
                if tool_name in seen:
                    continue
                seen.add(tool_name)
                schema = getattr(remote, "input_schema", None)
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                description = getattr(remote, "description", None) or "MCP-provided external tool."
                definition = ToolDefinition(tool_name, description, schema)
                adapted.append(MCPTool(client, server.name, remote_name, definition))
            await self.events.emit(
                MCPDiscoveryEvent(profile, label, "completed", tool_count=len(adapted))
            )
            return tuple(adapted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.events.emit(MCPDiscoveryEvent(profile, label, "failed", error=str(exc)))
            return ()

    @staticmethod
    def _display_name(name: str) -> str:
        return name.replace("_", " ").replace("-", " ").title()

    async def _connect(self, url: str) -> Any:
        if self._client_factory is None:
            try:
                module = cast(Any, importlib.import_module("mcp"))
            except ImportError as exc:
                raise RuntimeError(
                    "MCP support requires the optional dependency; install with "
                    "`pip install -e '.[mcp]'`."
                ) from exc
            self._client_factory = module.Client
        client = self._client_factory(url)
        await client.__aenter__()
        self._clients.append(client)
        return client

    async def close(self) -> None:
        clients, self._clients = self._clients, []
        errors: list[str] = []
        for client in reversed(clients):
            try:
                await client.__aexit__(None, None, None)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("MCP client shutdown failed: " + "; ".join(errors))

    def describe(self) -> str:
        try:
            servers = self.store.load()
        except ValueError as exc:
            return f"MCP configuration error: {exc}"
        if not servers:
            return "no MCP servers registered"
        return "\n".join(
            f"{server.name} · {self._safe_url(server.url)} · "
            f"{'all agents' if server.scope == 'global' else f'subagent {server.agent}'}"
            for server in servers
        )

    @staticmethod
    def _safe_url(url: str) -> str:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunparse(
            (parsed.scheme, host, parsed.path, "", "" if not parsed.query else "…", "")
        )
