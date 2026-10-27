from __future__ import annotations

import importlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import Any, cast

from harness.api.types import ToolDefinition, ToolResult

from .artifact_errors import InvalidArtifactError
from .artifact_validation import validate_artifact_definition
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
    root = artifact_root.resolve() / name
    try:
        root_info = root.lstat()
    except FileNotFoundError as exc:
        raise KeyError(name) from exc
    try:
        if artifact_root.is_symlink() or not S_ISDIR(root_info.st_mode):
            raise ValueError("artifact must be a real directory, not a symlink")
        manifest_info = (root / "manifest.json").lstat()
        if not S_ISREG(manifest_info.st_mode):
            raise ValueError("manifest.json must be a regular file, not a symlink or special file")
        config = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InvalidArtifactError(name, root, "missing manifest.json") from exc
    except json.JSONDecodeError as exc:
        raise InvalidArtifactError(name, root, f"invalid manifest JSON: {exc.msg}") from exc
    except UnicodeError as exc:
        raise InvalidArtifactError(name, root, "manifest.json must be valid UTF-8") from exc
    except IsADirectoryError as exc:
        raise InvalidArtifactError(name, root, "manifest.json must be a regular file") from exc
    except ValueError as exc:
        raise InvalidArtifactError(name, root, str(exc)) from exc
    if not isinstance(config, dict):
        raise InvalidArtifactError(name, root, "manifest must be an object")
    description = config.get("description")
    input_schema = config.get("input_schema")
    command = config.get("command")
    try:
        validate_artifact_definition(description, input_schema, command)
    except ValueError as exc:
        raise InvalidArtifactError(name, root, str(exc)) from exc
    assert isinstance(description, str)
    assert isinstance(input_schema, dict)
    assert isinstance(command, list)
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
