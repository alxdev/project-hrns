from __future__ import annotations

import sys

from dotenv import load_dotenv

from harness.agent import Agent
from harness.agent.loop import NullEventSink
from harness.commands import CommandContext, build_default_commands
from harness.compaction import CompactionManager
from harness.config import HarnessConfig, parse_config
from harness.permissions import AlwaysAllow, AlwaysAsk, AlwaysDeny
from harness.provider import AnthropicProvider, OpenAIProvider
from harness.tools.base import ToolContext
from harness.tools.bash import BashTool
from harness.tools.read_file import ReadFileTool
from harness.tools.registry import ToolRegistry
from harness.tools.write_file import WriteFileTool
from harness.runtime.noninteractive import run_once
from harness.runtime.status import format_runtime_configuration, format_startup_message


def build_provider(config: HarnessConfig):
    if config.provider_name == "openai":
        return OpenAIProvider(model=config.model)
    if config.provider_name == "anthropic":
        return AnthropicProvider(model=config.model)
    raise ValueError(f"unsupported provider: {config.provider_name}")


async def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    config = parse_config(argv)
    registry = ToolRegistry([ReadFileTool(), WriteFileTool(), BashTool()])
    commands = build_default_commands()
    provider = build_provider(config)
    compaction = CompactionManager(provider)

    if config.non_interactive:
        prompt = config.prompt if config.prompt is not None else sys.stdin.read()
        agent = Agent(
            provider=provider,
            tools=registry,
            permissions=AlwaysAllow() if config.allow_tools else AlwaysDeny(),
            context=ToolContext(config.working_directory),
            events=NullEventSink(),
            compaction=compaction,
        )
        return await run_once(
            agent,
            prompt,
            output=config.output,
            provider=config.provider_name,
            model=provider.model,
            working_directory=config.working_directory,
            compaction=compaction.strategy_name,
            tools_allowed=config.allow_tools,
        )

    from harness.ui.interactive import InteractiveRunner
    from harness.ui.prompt_toolkit_rich import TUI_NAME, create_ui

    input_adapter, output = create_ui(config.history_file)
    agent = Agent(
        provider=provider,
        tools=registry,
        permissions=AlwaysAsk(input_adapter.approve),
        context=ToolContext(config.working_directory),
        events=output,
        compaction=compaction,
    )
    output.write(
        format_startup_message(
            provider=config.provider_name,
            model=provider.model,
            working_directory=config.working_directory,
            compaction=compaction.strategy_name,
            tools=[definition.name for definition in registry.definitions()],
            tui=TUI_NAME,
        )
    )
    command_context = CommandContext(
        agent=agent,
        provider=provider,
        tools=registry,
        commands=commands,
        compaction=compaction,
        runtime_info=lambda: format_runtime_configuration(
            provider=config.provider_name,
            model=provider.model,
            working_directory=config.working_directory,
            compaction=compaction.strategy_name,
            tools=[definition.name for definition in registry.definitions()],
            tui=TUI_NAME,
        ),
    )
    await InteractiveRunner(input_adapter, output).run(agent, commands, command_context)
    return 0
