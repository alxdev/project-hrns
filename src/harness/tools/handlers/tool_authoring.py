from __future__ import annotations

import json
import os
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
from harness.tools.artifact_validation import (
    is_valid_artifact_name,
    portable_component,
    validate_artifact_definition,
)
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import load_artifact_tool
from harness.tools.results import capability_result


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


def _already_installed(name: str) -> ToolResult:
    return capability_result(
        "already_installed",
        f"tool {name!r} is already installed and protected; no creation attempted",
        "Reuse the installed tool through normal access and execution permissions.",
    )


def _intent(context: ToolContext, name: str) -> tuple[Path, Path, str]:
    assert context.artifact_root is not None
    return (
        context.artifact_root.absolute(),
        context.working_directory.absolute(),
        name,
    )


def _installed(context: ToolContext, name: str) -> ToolResult:
    context.artifact_authoring_intents.discard(_intent(context, name))
    return _already_installed(name)


def _root_identity(root: Path) -> tuple[int, int] | None:
    try:
        info = root.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def _invalid(exc: InvalidArtifactError) -> ToolResult:
    return capability_result(
        "existing_artifact_invalid",
        str(exc),
        "Call tool_authoring with only name and replace_invalid=true first. "
        "The harness must obtain archive/recreate consent before you prepare replacement code. "
        "Do not submit a full replacement against an invalid original.",
        is_error=True,
        name=exc.name,
        path=str(exc.path),
        manifest_path=str(exc.path / "manifest.json"),
        reason=exc.reason,
    )


async def prepare_recreation(context: ToolContext, error: InvalidArtifactError) -> ToolResult:
    """Consent and archive only; no replacement definition is accepted here."""
    artifact = error.path.absolute()
    root = artifact.parent
    if context.artifact_root is None or context.artifact_root.absolute() != root:
        return _invalid(error)
    async with artifact_lock(artifact):
        try:
            # Recheck under the lock through the shared validation cache.
            try:
                if context.tool_catalog is not None:
                    context.tool_catalog[error.name]
                else:
                    load_artifact_tool(root, error.name)
            except InvalidArtifactError as current:
                error = current
            except KeyError:
                return _failure("no existing invalid artifact to recreate", "not_installed")
            else:
                return _installed(context, error.name)
            before = _target_definition(artifact)
            target_info = artifact.lstat()
            root_info = root.stat()
            archive = archive_destination(artifact)
            details = {
                "name": error.name,
                "path": str(artifact),
                "archive_path": str(archive),
                "reason": error.reason,
            }
            approved_details = dict(details)
            approval = context.approve_artifact_replacement
            if approval is None or not await approval(
                ToolDefinition(
                    "recreate_invalid_artifact",
                    "Archive the invalid original now; prepare a replacement only after consent",
                    {"type": "object"},
                ),
                details,
            ):
                return _failure(
                    "explicit artifact replacement approval denied or unavailable",
                    "approval_denied",
                    name=error.name,
                    path=str(artifact),
                    reason=error.reason,
                )
            require_real_directory(root)
            if (
                details != approved_details
                or context.artifact_root.absolute() != root
                or not os.path.samestat(root.stat(), root_info)
                or not os.path.samestat(artifact.lstat(), target_info)
                or _target_definition(artifact) != before
            ):
                return _failure("artifact target changed during approval", "target_changed")
            rename_exclusive(artifact, archive)
        except Exception as exc:
            return _failure(
                f"artifact archive failed: {exc}",
                "archive_failed",
                name=error.name,
                path=str(artifact),
                reason=error.reason,
                original_archived=False,
            )
        context.archived_artifacts[error.name] = str(archive)
        context.artifact_authoring_intents.add(_intent(context, error.name))
        if context.tool_registry is not None:
            context.tool_registry.unregister(error.name)
        return capability_result(
            "original_archived",
            f"Invalid artifact {error.name!r}: {error.reason}. Original archived at {archive}.",
            "Now prepare a complete replacement using ordinary tool_authoring creation with "
            "replace_invalid=false, subject to normal creation approval. Execution requires "
            "separate approval. Do not retry automatically if creation fails.",
            name=error.name,
            reason=error.reason,
            path=str(artifact),
            archive_path=str(archive),
            original_archived=True,
            committed=False,
        )


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    arguments = deepcopy(arguments)
    name = arguments.get("name")
    if not isinstance(name, str) or not is_valid_artifact_name(name):
        return _failure(
            "name must start with a lowercase letter and contain only lowercase letters, "
            "digits, underscores, or hyphens; Windows reserved names are not allowed"
        )
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
    if replacing:
        context.artifact_authoring_intents.discard(_intent(context, name))
        if set(arguments) != {"name", "replace_invalid"}:
            return _failure(
                "archive/recreation requests accept only name and replace_invalid=true; "
                "obtain consent before preparing replacement code",
                "preparation_required",
            )
        try:
            if context.tool_catalog is not None:
                installed = context.tool_catalog.get(name)
            else:
                installed = load_artifact_tool(artifact.parent, name)
        except InvalidArtifactError as exc:
            return await prepare_recreation(context, exc)
        except KeyError:
            installed = None
        except ValueError as exc:
            return _failure(str(exc), "protected")
        except OSError as exc:
            return _failure(str(exc), "authoring_failed")
        if installed is not None:
            return _installed(context, name)
        return _failure("no existing invalid artifact to recreate", "not_installed")
    async with artifact_lock(artifact):
        state: dict[str, Any] = {"committed": False, "installed": False}
        if name in context.archived_artifacts:
            state["archive_path"] = context.archived_artifacts[name]
        try:
            result = await _author(context, arguments, artifact, state)
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
            if state["committed"]:
                context.artifact_authoring_intents.discard(_intent(context, name))
            owned = state.get("staging")
            cleanup_error = None
            retained = False
            if owned is not None:
                try:
                    retained = owned.cleanup()
                except Exception as exc:
                    # Cleanup must never hide the installation/archive result.
                    retained = True
                    cleanup_error = str(exc)
        if result.is_error and "archive_path" in state:
            payload = json.loads(result.content)
            for key in ("archive_path", "committed", "installed"):
                payload.setdefault(key, state[key])
            payload["original_archived"] = True
            payload["message"] += f" Original artifact archived at {state['archive_path']}."
            if not state["committed"]:
                payload["recovery_info"] = (
                    "The original was archived and no replacement installation committed. "
                    "The archive remains preserved; no rollback was performed."
                )
                payload["next_step"] = (
                    "Report the archived original and failure; do not retry automatically. "
                    "For a separately authorized corrected proposal, use ordinary "
                    "tool_authoring creation with "
                    "replace_invalid=false, subject to normal creation approval. "
                    "If the target is occupied, inspect it first; do not overwrite it or retry "
                    "archive/recreate automatically."
                )
                if payload["status"] != "validation_error":
                    payload["next_step"] += (
                        " Before preparing another proposal, request fresh preparation consent "
                        "with only name (replace_invalid=false)."
                    )
                payload["recovery_action"] = {
                    "tool": context.tool_name or "tool_authoring",
                    "name": name,
                    "replace_invalid": False,
                }
            result = replace(result, content=json.dumps(payload))
        if retained and owned is not None:
            payload = json.loads(result.content)
            payload["retained_stage_path"] = str(owned.root)
            payload["message"] += f" Inspect staging path {owned.root} for retained entries."
            if cleanup_error is not None:
                payload["cleanup_error"] = cleanup_error
                payload["message"] += " Staging cleanup failed; inspect the retained path."
            result = replace(result, content=json.dumps(payload))
        if result.is_error and json.loads(result.content)["status"] != "validation_error":
            context.artifact_authoring_intents.discard(_intent(context, name))
        return result


def _check_target(context: ToolContext, artifact: Path) -> ToolResult | None:
    name = artifact.name
    existing = context.tool_registry.get(name) if context.tool_registry is not None else None
    if existing is not None and not isinstance(existing, ArtifactTool):
        return _installed(context, name)
    if isinstance(existing, ArtifactTool) and existing.directory.resolve() != artifact:
        return _installed(context, name)
    try:
        _target_definition(artifact)
    except ValueError as exc:
        return _failure(str(exc), "protected")
    try:
        if context.tool_catalog is not None:
            installed = context.tool_catalog.get(name)
            if installed is not None:
                return _installed(context, name)
            if isinstance(context.tool_catalog, ToolCatalog):
                # The catalog already checked this artifact's current metadata.
                raise KeyError(name)
        load_artifact_tool(artifact.parent, name)
    except KeyError:
        if existing is not None:
            return _installed(context, name)
    except InvalidArtifactError as exc:
        context.artifact_authoring_intents.discard(_intent(context, name))
        return _invalid(exc)
    else:
        return _installed(context, name)
    return None


async def _author(
    context: ToolContext,
    arguments: dict[str, Any],
    artifact: Path,
    state: dict[str, Any],
) -> ToolResult:
    name = artifact.name
    checked = _check_target(context, artifact)
    if checked is not None:
        return checked

    intent = _intent(context, name)
    if set(arguments) <= {"name", "replace_invalid"}:
        context.artifact_authoring_intents.discard(intent)
        root_identity = _root_identity(artifact.parent)
        details = {
            "name": name,
            "artifact_root": str(intent[0]),
            "working_directory": str(intent[1]),
        }
        approved_details = dict(details)
        approval = context.approve_artifact_preparation
        if approval is None or not await approval(
            ToolDefinition(
                "prepare_artifact",
                "Authorize preparing a tool definition and files; "
                "installation requires separate approval",
                {"type": "object"},
            ),
            details,
        ):
            context.artifact_authoring_intents.discard(intent)
            return _failure(
                "tool artifact preparation approval denied or unavailable", "approval_denied"
            )
        if details != approved_details or _intent(context, name) != intent:
            return _failure("artifact preparation scope changed during approval", "target_changed")
        try:
            require_real_directory(artifact.parent)
        except ValueError as exc:
            return _failure(str(exc), "protected")
        checked = _check_target(context, artifact)
        if checked is not None:
            return checked
        if _root_identity(artifact.parent) != root_identity:
            return _failure("artifact root changed during approval", "target_changed")
        context.artifact_authoring_intents.add(intent)
        return capability_result(
            "preparation_authorized",
            f"Preparation authorized for tool {name!r} in this session.",
            "Now generate and submit the complete definition and files using tool_authoring "
            "with replace_invalid=false. Installation requires separate approval; "
            "preparation approval grants no writes or execution.",
            name=name,
            committed=False,
        )
    if intent not in context.artifact_authoring_intents:
        return _failure(
            "start with only name and obtain preparation consent "
            "before supplying a definition or files",
            "preparation_required",
        )

    # Policy has already allowed this native handler. Ask for creation only once
    # the read-only checks establish an actual creation attempt, never for a no-op.
    approval = context.approve_artifact_creation
    if approval is None:
        return _failure("tool artifact creation approval unavailable", "approval_denied")
    authoring = (
        context.tool_registry.get(context.tool_name or "tool_authoring")
        if context.tool_registry
        else None
    )
    definition = (
        authoring.definition
        if authoring is not None
        else ToolDefinition(
            context.tool_name or "tool_authoring", "Create a user-owned tool artifact", {}
        )
    )
    try:
        approved = await approval(definition, deepcopy(arguments))
    except BaseException:
        context.artifact_authoring_intents.discard(intent)
        raise
    if not approved:
        context.artifact_authoring_intents.discard(intent)
        return _failure("tool artifact creation approval denied", "approval_denied")

    # Keep the proposal separate from callback arguments and caller-owned dictionaries.
    proposal = deepcopy(
        {key: arguments.get(key) for key in ("description", "input_schema", "command", "files")}
    )
    root = artifact.parent
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
                portable_component(part) for part in filename.split("/")
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
    context.artifact_authoring_intents.discard(intent)

    # The proposal was validated once above. Writing and moving its files does
    # not require loading and validating the same definition again.
    tool = ArtifactTool(ToolDefinition(name, description, schema), artifact, tuple(command))
    if isinstance(context.tool_catalog, ToolCatalog):
        context.tool_catalog.cache_validated_artifact(tool)
    if context.tool_registry is not None:
        context.tool_registry.register(tool)
    if name in context.archived_artifacts and context.on_artifact_replaced is not None:
        context.on_artifact_replaced(tool)
    persisted = await context.persist_tool_access(name) if context.persist_tool_access else False
    success_details: dict[str, Any] = {
        "execution_verified": False,
        "committed": True,
        "installed": True,
        "path": str(artifact),
    }
    if "archive_path" in state:
        success_details["archive_path"] = state["archive_path"]
    return capability_result(
        "created",
        f"Created and activated tool artifact {name!r} "
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
