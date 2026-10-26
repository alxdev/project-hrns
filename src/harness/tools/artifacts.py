from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.api.types import ToolDefinition, ToolResult

from .base import ToolContext
from .process import run_process


@dataclass(frozen=True)
class ArtifactTool:
    definition: ToolDefinition
    directory: Path
    command: tuple[str, ...]

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
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
        result = await run_process(
            invocation,
            cwd=workspace,
            tool_name=self.definition.name,
            input_data=json.dumps(arguments, ensure_ascii=False).encode("utf-8"),
            env=environment,
            emit_log=context.emit_log,
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
