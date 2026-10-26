from __future__ import annotations

import json
import re
import shutil
from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext
from harness.tools.loader import load_artifact_tool

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    description = arguments.get("description")
    input_schema = arguments.get("input_schema")
    files = arguments.get("files")
    command = arguments.get("command")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return ToolResult("", "name must be a simple lowercase tool name", is_error=True)
    if not isinstance(description, str) or not description.strip():
        return ToolResult("", "description must be a non-empty string", is_error=True)
    if not isinstance(input_schema, dict):
        return ToolResult("", "input_schema must be an object", is_error=True)
    if (
        not isinstance(files, dict)
        or not files
        or not all(
            isinstance(path, str) and path.strip() and isinstance(content, str)
            for path, content in files.items()
        )
    ):
        return ToolResult("", "files must map relative file paths to text contents", is_error=True)
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part and "\0" not in part for part in command)
    ):
        return ToolResult(
            "", "command must be a non-empty array of command arguments", is_error=True
        )

    if context.artifact_root is None:
        return ToolResult("", "tool artifact storage is not configured", is_error=True)
    if context.tool_catalog is not None and name in context.tool_catalog:
        return ToolResult("", f"tool {name!r} is already installed", is_error=True)
    artifact_root = context.artifact_root.resolve()
    artifact = artifact_root / name
    if artifact.exists():
        return ToolResult("", f"tool artifact already exists: {artifact}", is_error=True)
    created = False
    try:
        artifact.mkdir(parents=True, exist_ok=False)
        created = True
        for relative_path, content in files.items():
            destination = (artifact / relative_path).resolve()
            destination.relative_to(artifact.resolve())
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        (artifact / "manifest.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "description": description,
                    "input_schema": input_schema,
                    "command": command,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tool = load_artifact_tool(artifact_root, name)
        if context.tool_registry is not None:
            context.tool_registry.register(tool)
    except Exception as exc:
        if created:
            shutil.rmtree(artifact, ignore_errors=True)
        return ToolResult("", f"tool artifact validation failed: {exc}", is_error=True)

    persisted = await context.persist_tool_access(name) if context.persist_tool_access else False
    persistence_note = (
        " Access was saved to the subagent profile."
        if persisted
        else " Access was not saved to the subagent profile."
    )

    return ToolResult(
        "",
        f"created and activated tool artifact {name!r} for this session at {artifact}. "
        "The command runs with JSON arguments on stdin; stdout is returned as the tool result. "
        "Other subagents can request this installed tool by name."
        f"{persistence_note}",
    )
