from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable

from harness.api.types import ToolDefinition, ToolResult

from .base import ToolContext
from .process import run_process
from .results import capability_result


@dataclass(frozen=True)
class ArtifactTool:
    definition: ToolDefinition
    directory: Path
    command: tuple[str, ...]

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            validator_for(self.definition.input_schema)(
                self.definition.input_schema,
                # An explicit empty registry refuses external retrieval. Omitting
                # this argument lets jsonschema use its legacy remote retriever.
                registry=Registry(),
            ).validate(arguments)
        except ValidationError as exc:
            # Messages, property names, and schema values can all contain secrets.
            # Report only the known validator keyword and a value-free location.
            location = f"<nested depth {min(len(exc.path), 99)}>" if exc.path else "<root>"
            keyword = str(exc.validator)[:40]
            return capability_result(
                "invalid_arguments",
                f"Invalid arguments at {location}: failed {keyword} constraint.",
                "Correct the supplied arguments to match the tool input_schema.",
                is_error=True,
            )
        except Unresolvable:
            return capability_result(
                "invalid_arguments",
                "Cannot resolve input_schema reference locally.",
                "Provide a schema whose references resolve locally; remote retrieval is disabled.",
                is_error=True,
            )
        workspace = context.working_directory.resolve()
        invocation = [self._argument(part, workspace) for part in self.command]
        skill_directories = {}
        if context.skill_catalog is not None:
            for skill_name in context.allowed_skills:
                skill = context.skill_catalog.get(skill_name)
                if skill is not None:
                    skill_directories[skill_name] = str(skill.root.resolve())
        environment = {
            **os.environ,
            "HARNESS_WORKSPACE_DIR": str(workspace),
            "HARNESS_ARTIFACT_DIR": str(self.directory),
            "HARNESS_SKILL_DIRS": json.dumps(skill_directories),
        }
        try:
            if not stat.S_ISDIR(workspace.stat().st_mode):
                raise NotADirectoryError(str(workspace))
        except OSError as exc:
            return capability_result(
                "working_directory_unavailable",
                f"Artifact working directory {str(workspace)!r} is unavailable "
                f"({type(exc).__name__}).",
                "Restore the configured working directory and its access permissions; "
                "do not retry automatically.",
                is_error=True,
                executable=invocation[0],
            )
        try:
            result = await run_process(
                invocation,
                cwd=workspace,
                tool_name=self.definition.name,
                input_data=json.dumps(arguments, ensure_ascii=False).encode("utf-8"),
                env=environment,
                emit_log=context.emit_log,
            )
        except OSError as exc:
            if exc.filename == str(workspace):
                status = "working_directory_unavailable"
            elif isinstance(exc, FileNotFoundError):
                status = "executable_unavailable"
            elif isinstance(exc, PermissionError):
                status = "launch_permission_denied"
            else:
                status = "launch_failed"
            return capability_result(
                status,
                f"Could not launch artifact executable argv[0]={invocation[0]!r} "
                f"in {str(workspace)!r} ({type(exc).__name__}, errno={exc.errno}).",
                "Check the executable path/runtime, working directory, and permissions. "
                "Keep the interpreter/executable and arguments in separate argv entries; "
                "the command is not shell text. Do not retry automatically.",
                is_error=True,
                executable=invocation[0],
                errno=exc.errno,
            )
        return result.as_tool_result()

    def _argument(self, part: str, workspace: Path) -> str:
        part = part.replace("{artifact_dir}", str(self.directory)).replace(
            "{workspace_dir}", str(workspace)
        )
        if not Path(part).is_absolute():
            try:
                candidate = (self.directory / part).resolve()
                if self.directory in candidate.parents and candidate.is_file():
                    return str(candidate)
            except OSError:
                # Inline source and other arguments need not be valid paths.
                pass
        return part
