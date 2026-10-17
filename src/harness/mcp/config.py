from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    url: str
    scope: str
    agent: str | None = None

    def applies_to(self, profile: str) -> bool:
        return self.scope == "global" or self.agent == profile


def normalize_server_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip()).strip("_-").lower()
    if not normalized:
        raise ValueError("server name must contain letters or numbers")
    return normalized


def server_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MCP URL must be an http or https URL with a hostname")
    return normalize_server_name(parsed.hostname.split(".")[0])


class MCPConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[MCPServerConfig]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("servers", []), list):
            raise ValueError("mcp.json must contain a 'servers' array")
        servers: list[MCPServerConfig] = []
        for index, item in enumerate(data.get("servers", [])):
            if not isinstance(item, dict):
                raise ValueError(f"mcp.json servers[{index}] must be an object")
            name, url, scope, agent = (
                item.get("name"),
                item.get("url"),
                item.get("scope"),
                item.get("agent"),
            )
            if not isinstance(name, str) or not isinstance(url, str):
                raise ValueError(f"mcp.json servers[{index}] requires string name and url")
            name = normalize_server_name(name)
            server_name_from_url(url)
            if scope == "global" and agent is None:
                servers.append(MCPServerConfig(name, url, "global"))
            elif scope == "agent" and isinstance(agent, str) and agent.strip():
                servers.append(MCPServerConfig(name, url, "agent", agent.strip()))
            else:
                raise ValueError(
                    f"mcp.json servers[{index}] scope must be global or agent with an agent name"
                )
        return servers

    def add(self, server: MCPServerConfig) -> None:
        servers = self.load()
        duplicate = any(
            item.name == server.name and item.scope == server.scope and item.agent == server.agent
            for item in servers
        )
        if duplicate:
            raise ValueError(f"MCP server is already registered for this scope: {server.name}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "name": item.name,
                "url": item.url,
                "scope": item.scope,
                **({"agent": item.agent} if item.agent is not None else {}),
            }
            for item in [*servers, server]
        ]
        self.path.write_text(json.dumps({"servers": entries}, indent=2) + "\n", encoding="utf-8")

    def remove(self, name: str) -> int:
        servers = self.load()
        remaining = [item for item in servers if item.name != normalize_server_name(name)]
        removed = len(servers) - len(remaining)
        if removed:
            entries = [
                {
                    "name": item.name,
                    "url": item.url,
                    "scope": item.scope,
                    **({"agent": item.agent} if item.agent is not None else {}),
                }
                for item in remaining
            ]
            self.path.write_text(
                json.dumps({"servers": entries}, indent=2) + "\n", encoding="utf-8"
            )
        return removed
