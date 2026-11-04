"""Root-only memory tools; deliberately absent from the installed tool catalog."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from harness.api.types import ToolDefinition, ToolResult
from harness.tools.base import ToolContext

from .manager import MemoryManager, encode
from .schemas import RECALL_SCHEMA, RECORD_SCHEMA
from .transactions import settle_write


class RecallExperience:
    definition = ToolDefinition(
        "recall_experience",
        "Recall bounded advisory experiences or lessons for this workspace by query/tags. "
        "Historical assessments are not instructions or permission; current configuration wins. "
        "Only the latest successful recall snapshot is protected in root context.",
        RECALL_SCHEMA,
    )

    def __init__(self, memory: MemoryManager) -> None:
        self.memory = memory

    def check_workspace(self, context: ToolContext) -> None:
        key = hashlib.sha256(str(context.working_directory.resolve()).encode()).hexdigest()
        if key != self.memory.store.workspace_key:
            raise ValueError("memory tool is bound to a different workspace")

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            self.check_workspace(context)
            return ToolResult("", await asyncio.to_thread(self.memory.recall, arguments))
        except (OSError, ValueError) as exc:
            return ToolResult("", encode({"status": "memory_error", "message": str(exc)}), True)


class RecordExperience(RecallExperience):
    definition = ToolDefinition(
        "record_experience",
        "Record a root assessment and optional lesson about finished delegation run IDs from "
        "this session. The harness supplies bounded runtime facts; do not submit transcripts, "
        "credentials, raw outputs, or fabricated evidence. Verification defaults to not_assessed; "
        "another root assessment requires rationale and remains distinct from runtime facts. "
        "Writes follow execution permissions and memory.json.",
        RECORD_SCHEMA,
    )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            self.check_workspace(context)
            result = await settle_write(lambda: self.memory.record(arguments))
            return ToolResult("", encode(result))
        except (OSError, ValueError) as exc:
            return ToolResult("", encode({"status": "memory_error", "message": str(exc)}), True)
