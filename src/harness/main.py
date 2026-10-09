from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from harness.agent import Agent
from harness.commands import CommandContext, build_default_commands
from harness.compaction import CompactionManager
from harness.permissions import AlwaysAsk
from harness.provider import AnthropicProvider, OpenAIProvider
from harness.tools.base import ToolContext
from harness.tools.bash import BashTool
from harness.tools.read_file import ReadFileTool
from harness.tools.registry import ToolRegistry
from harness.tools.write_file import WriteFileTool
from harness.ui.rich_terminal import RichTerminalUI


async def main() -> None:
    load_dotenv()
    ui = RichTerminalUI()
    registry = ToolRegistry([ReadFileTool(), WriteFileTool(), BashTool()])
    commands = build_default_commands()
    working_directory = Path(os.environ.get("HARNESS_WORKING_DIR", Path.cwd())).resolve()
    provider_name = os.environ.get("HARNESS_PROVIDER", "openai").lower()
    if provider_name == "openai":
        provider = OpenAIProvider()
    elif provider_name == "anthropic":
        provider = AnthropicProvider()
    else:
        raise ValueError(f"unsupported HARNESS_PROVIDER: {provider_name}")
    compaction = CompactionManager(provider)
    agent = Agent(
        provider=provider,
        tools=registry,
        permissions=AlwaysAsk(ui.approve),
        context=ToolContext(working_directory),
        events=ui,
        compaction=compaction,
    )
    ui.console.print(f"[bold cyan]Python Harness[/bold cyan] · model: {provider.model} · directory: {working_directory}")
    ui.console.print("Type [bold]/help[/bold] to see commands or [bold]/exit[/bold] to quit.")
    command_context = CommandContext(
        agent=agent,
        provider=provider,
        tools=registry,
        commands=commands,
        compaction=compaction,
    )
    await ui.run(agent, commands, command_context)
