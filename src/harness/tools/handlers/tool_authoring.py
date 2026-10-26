from __future__ import annotations

import json
import re
import shutil
from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext
from harness.tools.loader import load_artifact_tool
from harness.tools.results import capability_result

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _failure(message: str, status: str = "validation_error") -> ToolResult:
    return capability_result(
        status,
        message,
        "Report or correct the failure within the authorized scope; do not retry automatically.",
        is_error=True,
    )


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    description = arguments.get("description")
    input_schema = arguments.get("input_schema")
    files = arguments.get("files")
    command = arguments.get("command")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return _failure("name must be a simple lowercase tool name")
    if not isinstance(description, str) or not description.strip():
        return _failure("description must be a non-empty string")
    if not isinstance(input_schema, dict):
        return _failure("input_schema must be an object")
    if (
        not isinstance(files, dict)
        or not files
        or not all(
            isinstance(path, str) and path.strip() and isinstance(content, str)
            for path, content in files.items()
        )
    ):
        return _failure("files must map relative file paths to text contents")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part and "\0" not in part for part in command)
    ):
        return _failure("command must be a non-empty array of command arguments")

    if context.artifact_root is None:
        return _failure("tool artifact storage is not configured", "configuration_error")
    try:
        installed = (context.tool_catalog is not None and name in context.tool_catalog) or (
            context.tool_registry is not None and context.tool_registry.get(name) is not None
        )
    except (OSError, ValueError) as exc:
        return _failure(f"tool artifact validation failed: {exc}", "load_failed")
    if installed:
        return _failure(f"tool {name!r} is already installed", "already_installed")
    artifact_root = context.artifact_root.resolve()
    artifact = artifact_root / name
    if artifact.exists():
        return _failure(f"tool artifact already exists: {artifact}", "already_installed")
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
        return _failure(f"tool artifact validation failed: {exc}", "authoring_failed")

    persisted = await context.persist_tool_access(name) if context.persist_tool_access else False
    persistence_note = (
        " Access was saved to the subagent profile."
        if persisted
        else " Access was not saved to the subagent profile."
    )

    return capability_result(
        "created",
        f"created and activated tool artifact {name!r} for this session at {artifact}. "
        "The command runs with JSON arguments on stdin; stdout is returned as the tool result. "
        "Other subagents can request this installed tool by name."
        f"{persistence_note}",
        "Use the supplementary tool subject to execution policy.",
    )
