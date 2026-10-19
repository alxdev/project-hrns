from __future__ import annotations

import sys
from typing import Any

from dotenv import load_dotenv

from harness.agent import Agent
from harness.agent.loop import NullEventSink
from harness.api.types import ToolDefinition
from harness.commands import CommandContext, build_default_commands
from harness.compaction import CompactionManager
from harness.config import HarnessConfig, parse_config
from harness.mcp import MCPConfigStore, MCPToolManager
from harness.permissions import AlwaysAllow, AlwaysAsk, AlwaysDeny, RoutedPermissionPolicy
from harness.provider import AnthropicProvider, OpenAIProvider
from harness.runtime.noninteractive import run_once
from harness.runtime.operations import OperationController
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
from harness.transcript import Transcript, TranscriptEventSink


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
    transcript = Transcript()
    events: Any
    permissions: Any
    root_permissions: Any
    input_adapter: Any

    if config.non_interactive:
        events = TranscriptEventSink(transcript, NullEventSink())
        permissions = AlwaysAllow() if config.allow_tools else AlwaysDeny()
        root_permissions = AlwaysAllow()
        input_adapter = None
    else:
        from harness.ui.interactive import InteractiveRunner
        from harness.ui.prompt_toolkit_rich import TUI_NAME, create_ui

        input_adapter, output = create_ui(config.history_file)
        events = TranscriptEventSink(transcript, output)

        async def approve_tool(tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
            with output.suspend():
                return await input_adapter.approve(tool, arguments)

        permissions = AlwaysAsk(approve_tool)
        # Delegation is routing, not workspace execution. The subagent's
        # inherited policy still gates the actual read/write/shell operation.
        root_permissions = AlwaysAllow()

    base_tools: list[Tool] = [ReadFileTool(), WriteFileTool(), BashTool()]
    profiles = build_default_profiles(base_tools)
    mcp_manager = MCPToolManager(MCPConfigStore(config.working_directory / "mcp.json"), events)
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

    subagents = SubagentManager(
        create_subagent, profiles, events=events, tool_loader=mcp_manager.tools_for
    )
    delegation_tools = [
        DelegationTool(profile.name, profile.description) for profile in profiles.values()
    ]
    for delegation_tool in delegation_tools:
        delegation_tool.bind(subagents)
    # Keep the root agent as a strict planner/router. Workspace inspection,
    # file writes, and shell execution are available only through subagents.
    registry = ToolRegistry(delegation_tools)

    async def load_global_mcp_tools() -> None:
        for tool in await mcp_manager.tools_for("root"):
            if not any(item.name == tool.definition.name for item in registry.definitions()):
                registry.register(tool)

    agent = Agent(
        provider=provider,
        tools=registry,
        permissions=RoutedPermissionPolicy(root_permissions, {"mcp_": permissions}),
        context=context,
        events=events,
        compaction=compaction,
        name="root",
        before_send=load_global_mcp_tools,
    )

    if config.non_interactive:
        prompt = config.prompt if config.prompt is not None else sys.stdin.read()
        try:
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
        finally:
            await mcp_manager.close()

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
        transcript=transcript,
        runtime_info=lambda: format_runtime_configuration(
            provider=config.provider_name,
            model=provider.model,
            working_directory=config.working_directory,
            compaction=compaction.strategy_name,
            tools=[definition.name for definition in registry.definitions()],
            tui=TUI_NAME,
        ),
        mcp=mcp_manager,
    )
    try:
        await InteractiveRunner(input_adapter, output, OperationController()).run(
            agent, commands, command_context
        )
    finally:
        await mcp_manager.close()
    return 0
