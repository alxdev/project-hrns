from __future__ import annotations

import asyncio
import getpass
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from harness.api.types import ToolDefinition
from harness.model_selection import ModelOption
from harness.transcript import TranscriptEntry, TranscriptUpdateEvent


class TerminalUI:
    """The original minimal input/output terminal adapter."""

    def __init__(self) -> None:
        self._rendered_text_lengths: dict[str, int] = {}

    async def emit(self, event: object) -> None:
        if not isinstance(event, TranscriptUpdateEvent) or event.entry is None:
            return
        entry = event.entry
        if entry.kind == "assistant":
            self._render_assistant(entry, event.is_new)
        elif entry.kind == "tool_call":
            self._render_tool(entry, event.is_new)
        elif entry.kind == "subagent":
            self._render_subagent(entry, event.is_new)
        elif entry.kind == "mcp_discovery" and not event.is_new:
            label = "error" if entry.status == "failed" else "info"
            print(f"[{label}] {entry.content}")

    def _render_assistant(self, entry: TranscriptEntry, is_new: bool) -> None:
        if entry.status == "streaming":
            previous_length = self._rendered_text_lengths.get(entry.id, 0)
            delta = entry.content[previous_length:]
            if delta:
                print(delta, end="", flush=True)
            self._rendered_text_lengths[entry.id] = len(entry.content)
            return
        rendered_length = (
            self._rendered_text_lengths.pop(entry.id)
            if entry.id in self._rendered_text_lengths
            else None
        )
        if rendered_length is not None:
            print()
            if entry.status == "interrupted":
                print("[response interrupted]")
        elif is_new and entry.content:
            print(entry.content)

    def _render_tool(self, entry: TranscriptEntry, is_new: bool) -> None:
        prefix = str(entry.metadata.get("log_prefix", ""))
        if is_new:
            arguments = entry.metadata.get("arguments", {})
            print(f"{prefix}[tool] {entry.content} {json.dumps(arguments, sort_keys=True)}")
        elif entry.status in {"completed", "failed"}:
            label = "tool error" if entry.status == "failed" else "tool result"
            print(f"{prefix}[{label}] {entry.metadata.get('result', '')}")

    @staticmethod
    def _render_subagent(entry: TranscriptEntry, is_new: bool) -> None:
        if is_new:
            record_id = entry.metadata.get("record_id", "")
            print(f"↳ delegating to {entry.content} ({record_id})")
            return
        elapsed = entry.metadata.get("elapsed_seconds", 0.0)
        elapsed_seconds = elapsed if isinstance(elapsed, (int, float)) else 0.0
        print(f"← {entry.content} {entry.status} ({elapsed_seconds:.1f}s)")

    async def approve(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        answer = await asyncio.to_thread(
            input,
            f"Approve {tool.name} {json.dumps(arguments, sort_keys=True)}? [y/N] ",
        )
        return answer.strip().lower() in {"y", "yes"}

    async def prompt_secret(self, variable: str) -> str:
        return await asyncio.to_thread(
            getpass.getpass, f"Enter {variable} (kept only for this session): "
        )

    async def pick_model(
        self,
        options: Sequence[ModelOption],
        current: str | None = None,
    ) -> str | None:
        del current
        print("Available models:")
        for index, option in enumerate(options, start=1):
            status = "ready" if option.available else f"unavailable: {option.reason}"
            print(f"  {index}. {option.key} — {option.name} [{status}]")
        answer = await asyncio.to_thread(input, "Select a model number (blank to cancel): ")
        if not answer.strip():
            return None
        try:
            index = int(answer) - 1
            return list(options)[index].key
        except (ValueError, IndexError):
            return None

    async def read(self) -> str:
        return await asyncio.to_thread(input, "❯ ")

    def write(self, text: str) -> None:
        print(text)

    def error(self, text: str) -> None:
        print(f"[error] {text}", file=sys.stderr)

    class _Thinking:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
            return False

    def thinking(self) -> _Thinking:
        return self._Thinking()

    @contextmanager
    def suspend(self) -> Iterator[None]:
        yield
