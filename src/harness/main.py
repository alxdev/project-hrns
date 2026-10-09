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
from harness.ui.startup import format_runtime_configuration, print_startup_info


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
    print_startup_info(
        ui.console,
        provider=provider_name,
        model=provider.model,
        working_directory=working_directory,
        compaction=compaction.strategy_name,
        tools=[definition.name for definition in registry.definitions()],
    )
    command_context = CommandContext(
        agent=agent,
        provider=provider,
        tools=registry,
        commands=commands,
        compaction=compaction,
        runtime_info=lambda: format_runtime_configuration(
            provider=provider_name,
            model=provider.model,
            working_directory=working_directory,
            compaction=compaction.strategy_name,
            tools=[definition.name for definition in registry.definitions()],
        ),
    )
    await ui.run(agent, commands, command_context)
