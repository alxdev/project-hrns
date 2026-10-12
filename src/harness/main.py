from __future__ import annotations

import sys
from typing import Any

from dotenv import load_dotenv

from harness.agent import Agent
from harness.agent.loop import NullEventSink
from harness.commands import CommandContext, build_default_commands
from harness.compaction import CompactionManager
from harness.config import HarnessConfig, parse_config
from harness.permissions import AlwaysAllow, AlwaysAsk, AlwaysDeny
from harness.provider import AnthropicProvider, OpenAIProvider
from harness.runtime.noninteractive import run_once
from harness.runtime.status import format_runtime_configuration, format_startup_message
from harness.subagents import (
    DelegationTool,
    SubagentManager,
    SubagentProfile,
    build_default_profiles,
)
from harness.tools.base import Tool, ToolContext
from harness.tools.bash import BashTool
from harness.tools.read_file import ReadFileTool
from harness.tools.registry import ToolRegistry
from harness.tools.write_file import WriteFileTool


def build_provider(config: HarnessConfig) -> OpenAIProvider | AnthropicProvider:
    if config.provider_name == "openai":
        return OpenAIProvider(model=config.model)
    if config.provider_name == "anthropic":
        return AnthropicProvider(model=config.model)
    raise ValueError(f"unsupported provider: {config.provider_name}")


async def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    config = parse_config(argv)
    commands = build_default_commands()
    provider = build_provider(config)
    compaction = CompactionManager(provider)
    events: Any
    permissions: Any
    input_adapter: Any

    if config.non_interactive:
        events = NullEventSink()
        permissions = AlwaysAllow() if config.allow_tools else AlwaysDeny()
        input_adapter = None
    else:
        from harness.ui.interactive import InteractiveRunner
        from harness.ui.prompt_toolkit_rich import TUI_NAME, create_ui

        input_adapter, output = create_ui(config.history_file)
        events = output
        permissions = AlwaysAsk(input_adapter.approve)

    base_tools: list[Tool] = [ReadFileTool(), WriteFileTool(), BashTool()]
    profiles = build_default_profiles(base_tools)
    context = ToolContext(config.working_directory)

    def create_subagent(profile: SubagentProfile) -> Agent:
        subagent_provider = build_provider(config)
        subagent_provider.system = profile.system_prompt
        subagent_permissions = {
            "auto_allow": AlwaysAllow(),
            "inherit": permissions,
            "ask": permissions,
            "deny": AlwaysDeny(),
        }.get(profile.permission_mode, permissions)
        return Agent(
            provider=subagent_provider,
            tools=ToolRegistry(profile.tools),
            permissions=subagent_permissions,
            context=context,
            events=events,
            compaction=compaction,
            max_turns=profile.max_turns,
            name=profile.name,
            system_prompt=profile.system_prompt,
            log_prefix="  ↳ ",
            quiet=True,
        )

    subagents = SubagentManager(create_subagent, profiles, events=events)
    delegation_tools = [
        DelegationTool(profile.name, profile.description) for profile in profiles.values()
    ]
    for delegation_tool in delegation_tools:
        delegation_tool.bind(subagents)
    registry = ToolRegistry([*base_tools, *delegation_tools])
    agent = Agent(
        provider=provider,
        tools=registry,
        permissions=permissions,
        context=context,
        events=events,
        compaction=compaction,
        name="root",
    )

    if config.non_interactive:
        prompt = config.prompt if config.prompt is not None else sys.stdin.read()
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
        subagents=subagents,
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
