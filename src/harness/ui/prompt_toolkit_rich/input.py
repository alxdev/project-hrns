from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import DummyHistory, FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from harness.api.types import ToolDefinition


class PromptToolkitInput:
    """Input adapter implemented with prompt-toolkit."""

    def __init__(self, history_file: Path) -> None:
        self.session: Any = PromptSession(
            history=FileHistory(str(history_file)),
            enable_history_search=True,
        )

    async def read(self) -> str:
        with patch_stdout():
            return await self.session.prompt_async("❯ ")

    async def approve(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        prompt = f"Approve {tool.name} {json.dumps(arguments, sort_keys=True)}? [y/N] "
        with patch_stdout():
            answer = await self.session.prompt_async(prompt)
        return answer.strip().lower() in {"y", "yes"}

    async def prompt_secret(self, variable: str) -> str:
        with patch_stdout():
            secret_session: Any = PromptSession(history=DummyHistory())
            return await secret_session.prompt_async(
                f"Enter {variable} (kept only for this session): ", is_password=True
            )
