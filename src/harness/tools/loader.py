from __future__ import annotations

import importlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from harness.api.types import ToolDefinition, ToolResult

from .artifacts import ArtifactTool
from .base import Tool, ToolContext

ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]
_HANDLER_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ConfiguredTool:
    definition: ToolDefinition
    handler: ToolHandler

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        return await self.handler(replace(context, tool_name=self.definition.name), arguments)


def load_tools(path: Path) -> list[Tool]:
    """Load configured tool definitions and trusted Python handlers."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc.msg}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("tools"), dict):
        raise ValueError(f"{path} must contain a 'tools' object")
    return [_build_tool(path, name, config) for name, config in raw["tools"].items()]


def load_artifact_tool(artifact_root: Path, name: str) -> ArtifactTool:
    """Validate an artifact manifest without executing its command or files."""
    if not _HANDLER_NAME.fullmatch(name):
        raise ValueError("tool artifact names must be simple module names")
    root = (artifact_root.resolve() / name).resolve()
    try:
        root.relative_to(artifact_root.resolve())
    except ValueError as exc:
        raise ValueError("tool artifact escapes the artifact directory") from exc
    try:
        config = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"tool artifact {name!r} is missing manifest.json") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid tool artifact manifest for {name!r}: {exc.msg}") from exc
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read tool artifact {name!r}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"tool artifact {name!r} manifest must be an object")
    description = config.get("description")
    input_schema = config.get("input_schema")
    command = config.get("command")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"tool artifact {name!r} requires a description")
    if not isinstance(input_schema, dict):
        raise ValueError(f"tool artifact {name!r} requires an input_schema object")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part and "\0" not in part for part in command)
    ):
        raise ValueError(f"tool artifact {name!r} requires a non-empty command array")
    return ArtifactTool(ToolDefinition(name, description, input_schema), root, tuple(command))


def _build_tool(path: Path, name: object, config: object) -> ConfiguredTool:
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError(f"{path} tool names must be non-empty trimmed strings")
    if not isinstance(config, dict):
        raise ValueError(f"{path} tool {name!r} must be an object")
    description = _required_string(path, name, config, "description")
    handler_name = _required_string(path, name, config, "handler")
    if not _HANDLER_NAME.fullmatch(handler_name):
        raise ValueError(f"{path} tool {name!r} handler must be a simple Python module name")
    input_schema = config.get("input_schema")
    if not isinstance(input_schema, dict):
        raise ValueError(f"{path} tool {name!r} input_schema must be an object")
    module_name = f"harness.tools.handlers.{handler_name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise ValueError(f"{path} tool {name!r} handler not found: {handler_name}.py") from exc
        raise
    handler = getattr(module, "execute", None)
    if not callable(handler):
        raise ValueError(f"{path} handler {handler_name}.py must define async execute()")
    return ConfiguredTool(
        ToolDefinition(name, description, input_schema), cast(ToolHandler, handler)
    )


def _required_string(path: Path, name: str, config: dict[str, Any], field: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} tool {name!r} requires a non-empty string {field}")
    return value
