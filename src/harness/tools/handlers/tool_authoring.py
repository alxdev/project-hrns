from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from stat import S_ISREG
from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.artifact_recovery import (
    OwnedStaging,
    archive_destination,
    artifact_lock,
    rename_exclusive,
    require_real_directory,
)
from harness.tools.artifact_validation import validate_artifact_definition
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import load_artifact_tool
from harness.tools.results import capability_result

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _portable_component(part: str) -> bool:
    return (
        bool(part)
        and part not in (".", "..")
        and not part.endswith((".", " "))
        and not any(c in '<>:"/\\|?*' or ord(c) < 32 for c in part)
        and part.split(".")[0].casefold() not in _RESERVED
    )


def _target_definition(artifact: Path) -> bytes | None:
    """Check only the exact target and definition, never traverse its contents."""
    require_real_directory(artifact)
    manifest = artifact / "manifest.json"
    try:
        info = manifest.lstat()
    except FileNotFoundError:
        return None
    if not S_ISREG(info.st_mode):
        raise ValueError("manifest.json must be a regular file, not a symlink or special file")
    return manifest.read_bytes()


def _failure(message: str, status: str = "validation_error", **details: Any) -> ToolResult:
    return capability_result(
        status,
        message,
        "Report or correct the failure within the authorized scope; do not retry automatically.",
        is_error=True,
        **details,
    )


def _invalid(exc: InvalidArtifactError) -> ToolResult:
    return capability_result(
        "existing_artifact_invalid",
        str(exc),
        "Use tool_authoring with replace_invalid=true and a complete replacement. "
        "Separate explicit user approval is required before preserving and replacing the original.",
        is_error=True,
        name=exc.name,
        path=str(exc.path),
        manifest_path=str(exc.path / "manifest.json"),
        reason=exc.reason,
    )


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    name = arguments.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name) or not _portable_component(name):
        return _failure("name must be a simple lowercase tool name")
    replacing = arguments.get("replace_invalid", False)
    if not isinstance(replacing, bool):
        return _failure("replace_invalid must be a boolean")
    if context.artifact_root is None:
        return _failure("tool artifact storage is not configured", "configuration_error")
    try:
        require_real_directory(context.artifact_root.absolute())
    except ValueError as exc:
        return _failure(str(exc), "protected")
    except OSError as exc:
        return _failure(str(exc), "authoring_failed")
    artifact = context.artifact_root.absolute() / name
    async with artifact_lock(artifact):
        state: dict[str, Any] = {"committed": False, "installed": False}
        try:
            result = await _author(context, arguments, artifact, replacing, state)
        except Exception as exc:
            result = _failure(
                f"tool artifact authoring failed: {exc}",
                "post_install_failed" if state["committed"] else "authoring_failed",
                **{key: value for key, value in state.items() if key != "staging"},
                path=str(artifact),
                execution_verified=False,
                recovery_info=(
                    "Installation committed. Inspect the installed path and preserved archive; "
                    "refresh/persistence may be incomplete. Do not recreate or retry automatically."
                    if state["committed"]
                    else "No installation committed. Inspect any reported preserved archive."
                ),
            )
        finally:
            owned = state.get("staging")
            retained = owned.cleanup() if owned is not None else False
        if retained and owned is not None:
            payload = json.loads(result.content)
            payload["retained_stage_path"] = str(owned.root)
            payload["message"] += f" Changed or unknown staging entries retained at {owned.root}."
            result = replace(result, content=json.dumps(payload))
        return result


async def _author(
    context: ToolContext,
    arguments: dict[str, Any],
    artifact: Path,
    replacing: bool,
    state: dict[str, Any],
) -> ToolResult:
    name = artifact.name
    old_error = None
    existing = context.tool_registry.get(name) if context.tool_registry is not None else None
    if existing is not None and not isinstance(existing, ArtifactTool):
        return _failure(f"tool {name!r} is protected", "already_installed")
    if isinstance(existing, ArtifactTool) and existing.directory.resolve() != artifact:
        return _failure(f"tool {name!r} is protected", "already_installed")
    try:
        _target_definition(artifact)
    except ValueError as exc:
        return _failure(str(exc), "protected")
    try:
        if context.tool_catalog is not None:
            installed = context.tool_catalog.get(name)
            if installed is not None:
                return _failure(
                    f"tool {name!r} is already installed and protected", "already_installed"
                )
            if isinstance(context.tool_catalog, ToolCatalog):
                # The catalog already checked this artifact's current metadata.
                raise KeyError(name)
        load_artifact_tool(artifact.parent, name)
    except KeyError:
        if replacing:
            return _failure("no existing invalid artifact to recreate", "not_installed")
        if existing is not None:
            return _failure(f"tool {name!r} is already installed", "already_installed")
    except InvalidArtifactError as exc:
        if not replacing:
            return _invalid(exc)
        old_error = exc
    else:
        return _failure(f"tool {name!r} is already installed and protected", "already_installed")

    # Keep the proposal separate from callback arguments and caller-owned dictionaries.
    proposal = deepcopy(
        {key: arguments.get(key) for key in ("description", "input_schema", "command", "files")}
    )
    root = artifact.parent
    archive = None
    if old_error is not None:
        before = _target_definition(artifact)
        target_info = artifact.lstat()
        root_info = root.stat()
        archive = archive_destination(artifact)
        details = {
            "name": name,
            "path": str(artifact),
            "archive_path": str(archive),
            "reason": old_error.reason,
            "replacement": deepcopy(proposal),
        }
        approved_details = deepcopy(details)
        approval = context.approve_artifact_replacement
        if approval is None or not await approval(
            ToolDefinition(
                "recreate_invalid_artifact",
                "Archive the invalid original, then validate and create the replacement",
                {"type": "object"},
            ),
            details,
        ):
            return _failure(
                "explicit artifact replacement approval denied or unavailable",
                "approval_denied",
            )
        if details != approved_details:
            return _failure("replacement proposal changed during approval", "target_changed")
        try:
            require_real_directory(root)
            if (
                context.artifact_root is None
                or context.artifact_root.absolute() != root
                or not os.path.samestat(root.stat(), root_info)
                or not os.path.samestat(artifact.lstat(), target_info)
                or _target_definition(artifact) != before
            ):
                return _failure("artifact target changed during approval", "target_changed")
        except (KeyError, OSError, ValueError):
            return _failure("artifact target changed during approval", "target_changed")
        # Preserve the original first, even if ordinary replacement validation fails.
        rename_exclusive(artifact, archive)
        state["archive_path"] = str(archive)

    description = proposal["description"]
    schema = proposal["input_schema"]
    command = proposal["command"]
    files = proposal["files"]
    try:
        validate_artifact_definition(description, schema, command)
        if not isinstance(files, dict) or not files:
            raise ValueError("files must map relative file paths to text contents")
        paths: set[tuple[str, ...]] = set()
        for filename, content in files.items():
            if not isinstance(filename, str) or not all(
                _portable_component(part) for part in filename.split("/")
            ):
                raise ValueError("files require portable relative paths inside the artifact")
            parts = tuple(part.casefold() for part in filename.split("/"))
            if parts[0] == "manifest.json":
                raise ValueError("manifest.json is reserved for the generated definition")
            if not isinstance(content, str):
                raise ValueError("files must map relative file paths to text contents")
            if any(parts == other[: len(parts)] or other == parts[: len(other)] for other in paths):
                raise ValueError("file paths must not overlap or alias")
            paths.add(parts)
    except ValueError as exc:
        return _failure(
            str(exc),
            path=str(artifact),
            execution_verified=False,
            **{key: value for key, value in state.items() if key != "staging"},
        )

    # Narrow the types established by the shared validator for construction.
    assert isinstance(description, str)
    assert isinstance(schema, dict)
    assert isinstance(command, list)
    require_real_directory(root)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".harness-stage-", dir=root))
    owned = OwnedStaging(staging)
    state["staging"] = owned
    staged = staging / name
    owned.mkdir(Path(name))
    for filename, content in files.items():
        owned.write(Path(name) / filename, content)
    manifest = {
        "name": name,
        "description": description,
        "input_schema": schema,
        "command": command,
    }
    owned.write(Path(name) / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    rename_exclusive(staged, artifact)
    state["committed"] = True
    state["installed"] = True

    # The proposal was validated once above. Writing and moving its files does
    # not require loading and validating the same definition again.
    tool = ArtifactTool(ToolDefinition(name, description, schema), artifact, tuple(command))
    if isinstance(context.tool_catalog, ToolCatalog):
        context.tool_catalog.cache_validated_artifact(tool)
    if context.tool_registry is not None:
        if old_error is not None:
            context.tool_registry.unregister(name)
        context.tool_registry.register(tool)
    if old_error is not None and context.on_artifact_replaced is not None:
        context.on_artifact_replaced(tool)
    persisted = await context.persist_tool_access(name) if context.persist_tool_access else False
    success_details: dict[str, Any] = {
        "execution_verified": False,
        "committed": True,
        "installed": True,
        "path": str(artifact),
    }
    if archive is not None:
        success_details["archive_path"] = str(archive)
    return capability_result(
        "recreated" if old_error is not None else "created",
        f"{'Recreated' if old_error else 'Created'} and activated tool artifact {name!r} "
        f"at {artifact}. Definition validated; not executed/functional behavior not verified. "
        + (
            "Access was saved to the subagent profile."
            if persisted
            else "Access was not saved to the subagent profile."
        ),
        "Functional verification requires a separately approved execution. "
        "Run representative input and compare the output with the requested result "
        "before claiming the task complete.",
        **success_details,
    )
