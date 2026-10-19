from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlparse


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    url: str
    scope: str
    agent: str | None = None
    auth: dict[str, object] | None = None

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
            name, url, scope, agent, auth = (
                item.get("name"),
                item.get("url"),
                item.get("scope"),
                item.get("agent"),
                item.get("auth"),
            )
            if not isinstance(name, str) or not isinstance(url, str):
                raise ValueError(f"mcp.json servers[{index}] requires string name and url")
            name = normalize_server_name(name)
            server_name_from_url(url)
            if auth is not None and not isinstance(auth, dict):
                raise ValueError(f"mcp.json servers[{index}] auth must be an object")
            if auth is not None:
                _validate_auth(auth, index)
            if scope == "global" and agent is None:
                servers.append(MCPServerConfig(name, url, "global", auth=auth))
            elif scope == "agent" and isinstance(agent, str) and agent.strip():
                servers.append(MCPServerConfig(name, url, "agent", agent.strip(), auth))
            else:
                raise ValueError(
                    f"mcp.json servers[{index}] scope must be global or agent with an agent name"
                )
        return servers

    def add(self, server: MCPServerConfig) -> None:
        if server.auth is not None:
            _validate_auth(server.auth, 0)
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
                **({"auth": item.auth} if item.auth is not None else {}),
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
                    **({"auth": item.auth} if item.auth is not None else {}),
                }
                for item in remaining
            ]
            self.path.write_text(
                json.dumps({"servers": entries}, indent=2) + "\n", encoding="utf-8"
            )
        return removed


def _validate_auth(auth: dict[str, object], index: int) -> None:
    kind = auth.get("type")
    if kind not in {"bearer", "headers", "oauth"}:
        raise ValueError(f"mcp.json servers[{index}] auth.type must be bearer, headers, or oauth")

    env_fields = {
        "bearer": ("token_env",),
        "headers": (),
        "oauth": ("client_id_env",),
    }[kind]
    for field in env_fields:
        value = auth.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"mcp.json servers[{index}] auth.{field} must name an env var")

    if kind == "headers":
        headers = auth.get("headers")
        if not isinstance(headers, dict) or not headers:
            raise ValueError(f"mcp.json servers[{index}] auth.headers must be a non-empty object")
        if any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()
        ):
            raise ValueError(f"mcp.json servers[{index}] auth.headers values must name env vars")
    if kind == "oauth":
        secret_env = auth.get("client_secret_env")
        if secret_env is not None and (not isinstance(secret_env, str) or not secret_env.strip()):
            raise ValueError(
                f"mcp.json servers[{index}] auth.client_secret_env must name an env var"
            )
        token_auth_method = auth.get(
            "token_auth_method", "client_secret_post" if secret_env else "none"
        )
        if token_auth_method not in {"none", "client_secret_basic", "client_secret_post"}:
            raise ValueError(f"mcp.json servers[{index}] auth.token_auth_method is unsupported")
        if token_auth_method != "none" and not secret_env:
            raise ValueError(
                f"mcp.json servers[{index}] auth.client_secret_env is required for this method"
            )
        redirect_uri = auth.get("redirect_uri")
        if not isinstance(redirect_uri, str) or not redirect_uri.startswith("http://127.0.0.1:"):
            raise ValueError(
                f"mcp.json servers[{index}] OAuth redirect_uri must use http://127.0.0.1:<port>"
            )
        scopes = auth.get("scopes", [])
        if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
            raise ValueError(f"mcp.json servers[{index}] auth.scopes must be an array of strings")


def resolve_auth_headers(auth: dict[str, object] | None) -> dict[str, str]:
    """Resolve environment-backed static headers without persisting secret values."""
    if auth is None:
        return {}
    kind = auth["type"]
    if kind == "bearer":
        variable = str(auth["token_env"])
        token = os.environ.get(variable)
        if not token:
            raise ValueError(f"required environment variable {variable} is not set")
        return {"Authorization": f"Bearer {token}"}
    if kind == "headers":
        result: dict[str, str] = {}
        header_env = cast(dict[str, str], auth["headers"])
        for header, variable in header_env.items():
            value = os.environ.get(variable)
            if not value:
                raise ValueError(f"required environment variable {variable} is not set")
            result[header] = value
        return result
    return {}
