from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class CommandContext:
    agent: Any
    provider: Any
    tools: Any
    commands: CommandRegistry | None = None
    compaction: Any = None
    subagents: Any = None
    transcript: Any = None
    runtime_info: Callable[[], str] | None = None
    mcp: Any = None
    model_catalog: Any = None
    select_model: Callable[[str], str] | None = None
    current_model_key: Callable[[], str | None] | None = None
    model_picker: Callable[[], Awaitable[str]] | None = None
    project_context: Any = None
    memory: Any = None


@dataclass(frozen=True)
class CommandResult:
    text: str = ""
    should_exit: bool = False
    action: Callable[[], Awaitable[str]] | None = None


CommandHandler = Callable[[str, CommandContext], CommandResult]


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    usage: str
    handler: CommandHandler


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(
        self,
        name: str,
        description: str,
        handler: CommandHandler,
        usage: str | None = None,
    ) -> None:
        normalized = name.removeprefix("/").strip().lower()
        if not normalized:
            raise ValueError("command name cannot be empty")
        if normalized in self._commands:
            raise ValueError(f"command already registered: /{normalized}")
        self._commands[normalized] = Command(
            name=normalized,
            description=description,
            usage=usage or f"/{normalized}",
            handler=handler,
        )

    def commands(self) -> list[Command]:
        return [self._commands[name] for name in sorted(self._commands)]

    def dispatch(self, line: str, context: CommandContext) -> CommandResult | None:
        stripped = line.strip()
        if not stripped.startswith("/"):
            return None
        parts = stripped[1:].split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        args = parts[1].strip() if len(parts) == 2 else ""
        command = self._commands.get(name)
        if command is None:
            return CommandResult(f"unknown command: /{name} (try /help)")
        return command.handler(args, context)
