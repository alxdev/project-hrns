from __future__ import annotations

import importlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import Tool, ToolContext

ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]
_HANDLER_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ConfiguredTool:
    definition: ToolDefinition
    handler: ToolHandler

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        return await self.handler(context, arguments)


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

    definition = ToolDefinition(
        name=name,
        description=description,
        input_schema=input_schema,
    )
    return ConfiguredTool(definition, cast(ToolHandler, handler))


def _required_string(path: Path, name: str, config: dict[str, Any], field: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} tool {name!r} requires a non-empty string {field}")
    return value
