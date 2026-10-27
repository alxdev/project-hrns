from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from harness.agent import Agent
from harness.agent.loop import NullEventSink, ToolLogEvent
from harness.api.types import ToolDefinition
from harness.commands import CommandContext, build_default_commands
from harness.compaction import CompactionManager
from harness.config import HarnessConfig, parse_config
from harness.mcp import MCPConfigStore, MCPToolManager
from harness.model_catalog import ModelCatalog, ModelChoice, build_provider_for_choice
from harness.model_selection import MODEL_PICKER_EXIT, ModelOption
from harness.permissions import AlwaysAllow, AlwaysAsk, AlwaysDeny, RoutedPermissionPolicy
from harness.project_context import ProjectContext, load_project_context
from harness.provider import (
    AnthropicProvider,
    LocalProvider,
    OpenAIProvider,
)
from harness.runtime.noninteractive import run_once
from harness.runtime.operations import OperationController
from harness.runtime.status import format_runtime_configuration, format_startup_message
from harness.skills import SkillError, load_skills
from harness.subagents import (
    DelegationTool,
    SubagentManager,
    SubagentProfile,
    load_profiles,
)
from harness.subagents.instructions import assemble_system_prompt
from harness.tools.access import make_profile_tool_access_persister
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import load_tools
from harness.tools.registry import ToolRegistry
from harness.transcript import Transcript, TranscriptEventSink
from harness.ui.protocols import InputAdapter, OutputRenderer


def build_provider(
    config: HarnessConfig,
    project_context: ProjectContext | None = None,
) -> OpenAIProvider | AnthropicProvider | LocalProvider:
    provider: OpenAIProvider | AnthropicProvider | LocalProvider
    if config.provider_name == "openai":
        provider = OpenAIProvider(model=config.model)
    elif config.provider_name == "anthropic":
        provider = AnthropicProvider(model=config.model)
    elif config.provider_name == "local":
        provider = LocalProvider(model=config.model)
    else:
        raise ValueError(f"unsupported provider: {config.provider_name}")
    if project_context is not None:
        provider.system = project_context.system_prompt(provider.system)
    return provider


async def choose_startup_model(
    input_adapter: InputAdapter,
    output: OutputRenderer,
    model_catalog: ModelCatalog,
    config: HarnessConfig,
    project_context: ProjectContext,
) -> tuple[Any, ModelChoice] | None:
    """Show the startup model picker and build the selected provider."""
    options = list(model_catalog.statuses())
    option_by_key = {option.key: option for option in options}
    if not any(option.available for option in options):
        options.append(ModelOption(MODEL_PICKER_EXIT, "Exit application", True))
    current = model_catalog.preferred_key(config.provider_name, config.model)
    if current is None or not option_by_key[current].available:
        current = next(
            (option.key for option in options if option.available),
            MODEL_PICKER_EXIT,
        )
    while True:
        selected = await input_adapter.pick_model(options, current)
        if selected is None or selected == MODEL_PICKER_EXIT:
            return None
        option = option_by_key.get(selected)
        if option is None:
            output.error(f"unknown startup model: {selected}")
            continue
        if not option.available:
            output.error(f"{option.key} is unavailable: {option.reason}")
            continue
        choice = model_catalog.find(selected)
        try:
            provider = build_provider_for_choice(choice)
            provider.system = project_context.system_prompt(provider.system)
            return provider, choice
        except ValueError as exc:
            output.error(f"{option.key} is unavailable: {exc}")


async def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    config = parse_config(argv)
    project_context = load_project_context(config.working_directory)
    model_catalog = ModelCatalog.load(Path.cwd())
    commands = build_default_commands()
    current_model_key: str | None = None
    current_choice: ModelChoice | None = None
    transcript = Transcript()
    events: Any
    permissions: Any
    root_permissions: Any
    input_adapter: Any
    output: Any = None

    if config.non_interactive:
        try:
            provider = build_provider(config, project_context)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        events = TranscriptEventSink(transcript, NullEventSink())
        permissions = AlwaysAllow() if config.allow_tools else AlwaysDeny()
        root_permissions = AlwaysAllow()
        input_adapter = None
    else:
        from harness.ui.interactive import InteractiveRunner
        from harness.ui.prompt_toolkit_rich import TUI_NAME, create_ui

        input_adapter, output = create_ui(config.history_file)
        selected_startup = await choose_startup_model(
            input_adapter, output, model_catalog, config, project_context
        )
        if selected_startup is None:
            output.write("startup cancelled")
            return 0
        provider, current_choice = selected_startup
        current_model_key = current_choice.key
        events = TranscriptEventSink(transcript, output)

        async def approve_tool(tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
            with output.suspend():
                return await input_adapter.approve(tool, arguments)

        permissions = AlwaysAsk(approve_tool)
        # Delegation is routing, not workspace execution. The subagent's
        # inherited policy still gates the actual read/write/shell operation.
        root_permissions = AlwaysAllow()

    compaction = CompactionManager(provider)

    try:
        base_tools = load_tools(config.working_directory / "tools.json")
    except ValueError as exc:
        message = f"invalid tools.json: {exc}"
        if output is None:
            print(message, file=sys.stderr)
        else:
            output.error(message)
        return 2
    try:
        skill_catalog = load_skills(
            config.working_directory / "skills.json",
            config.working_directory,
        )
    except SkillError as exc:
        message = f"invalid skills configuration: {exc}"
        if output is None:
            print(message, file=sys.stderr)
        else:
            output.error(message)
        return 2
    artifact_root = config.data_directory / "tool-artifacts"
    tool_catalog = ToolCatalog(base_tools, artifact_root)
    try:
        profiles = load_profiles(config.working_directory / "subagents.json", tool_catalog)
    except ValueError as exc:
        message = f"invalid subagents.json: {exc}"
        if output is None:
            print(message, file=sys.stderr)
        else:
            output.error(message)
        return 2
    missing_skills = {
        skill_name
        for profile in profiles.values()
        for skill_name in profile.skills
        if skill_catalog.get(skill_name) is None
    }
    if missing_skills:
        message = f"unknown skills in subagents.json: {', '.join(sorted(missing_skills))}"
        if output is None:
            print(message, file=sys.stderr)
        else:
            output.error(message)
        return 2

    async def prompt_mcp_secret(variable: str) -> str:
        if input_adapter is None or output is None:
            raise ValueError(
                f"missing credential for {variable}; provide it through the environment "
                "when running non-interactively"
            )
        with output.suspend():
            return await input_adapter.prompt_secret(variable)

    mcp_manager = MCPToolManager(
        MCPConfigStore(config.working_directory / "mcp.json"),
        events,
        prompt_secret=prompt_mcp_secret if input_adapter is not None else None,
    )
    context = ToolContext(config.working_directory)

    async def approve_artifact_replacement(tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        if config.non_interactive or input_adapter is None or output is None:
            return False
        with output.suspend():
            return await input_adapter.approve(tool, arguments)

    def create_subagent(profile: SubagentProfile) -> Agent:
        subagent_provider = (
            build_provider_for_choice(current_choice)
            if current_choice is not None
            else build_provider(config)
        )
        profile_skills = skill_catalog.for_names(profile.skills)
        skill_prompt = profile_skills.catalog_prompt()
        subagent_provider.system = assemble_system_prompt(profile.system_prompt, skill_prompt)
        subagent_permissions = {
            "auto_allow": AlwaysAllow(),
            "inherit": permissions,
            "ask": permissions,
            "deny": AlwaysDeny(),
        }.get(profile.permission_mode, permissions)
        registry = ToolRegistry(profile.tools)

        async def emit_log(tool_name: str, text: str, stream: str) -> None:
            await events.emit(
                ToolLogEvent(
                    name=tool_name,
                    text=text,
                    stream=stream,
                    agent=profile.name,
                    log_prefix="  ↳ ",
                )
            )

        async def approve_persist_tool(tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
            if input_adapter is None or output is None:
                return False
            with output.suspend():
                return await input_adapter.approve(tool, arguments)

        persist_tool_access = make_profile_tool_access_persister(
            config.working_directory / "subagents.json",
            profile.name,
            approve_persist_tool,
            tool_catalog,
            report_error=output.error if output is not None else None,
            # The factory runs only after the manager has been initialized.
            on_saved=lambda name: subagents.grant_tool_access(profile.name, tool_catalog[name]),
        )

        agent_context = replace(
            context,
            allowed_read_roots=tuple(skill.root for skill in profile_skills.skills.values()),
            skill_catalog=skill_catalog,
            allowed_skills=profile.skills,
            tool_registry=registry,
            persist_tool_access=persist_tool_access,
            emit_log=emit_log,
            artifact_root=artifact_root,
            tool_catalog=tool_catalog,
            approve_artifact_replacement=approve_artifact_replacement,
            on_artifact_replaced=lambda tool: subagents.refresh_artifact_access(tool),
        )
        return Agent(
            provider=subagent_provider,
            tools=registry,
            permissions=subagent_permissions,
            context=agent_context,
            events=events,
            compaction=compaction,
            max_turns=profile.max_turns,
            name=profile.name,
            system_prompt=subagent_provider.system,
            log_prefix="  ↳ ",
            quiet=True,
        )

    def report_unavailable(message: str) -> None:
        if output is None:
            print(message, file=sys.stderr)
        else:
            output.error(message)

    subagents = SubagentManager(
        create_subagent,
        profiles,
        events=events,
        tool_loader=mcp_manager.tools_for,
        report_unavailable=report_unavailable,
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

    def select_model(identifier: str) -> str:
        nonlocal current_choice, current_model_key, provider
        choice = model_catalog.find(identifier)
        selected_provider = build_provider_for_choice(choice)
        selected_provider.system = provider.system
        provider = selected_provider
        current_choice = choice
        current_model_key = choice.key
        agent.provider = selected_provider
        compaction.set_provider(selected_provider)
        command_context.provider = selected_provider
        return f"model: {choice.key} — {choice.name}"

    def active_provider_name() -> str:
        return current_choice.provider if current_choice is not None else config.provider_name

    def active_model_key() -> str | None:
        return current_model_key

    async def pick_model() -> str:
        if input_adapter is None or output is None:
            return model_catalog.describe(current_model_key)
        options = list(model_catalog.statuses())
        with output.suspend():
            selected = await input_adapter.pick_model(options, current_model_key)
        if selected is None:
            return "model selection cancelled"
        try:
            return select_model(selected)
        except (ValueError, RuntimeError) as exc:
            return f"model selection failed: {exc}"

    if config.non_interactive:
        prompt = config.prompt if config.prompt is not None else sys.stdin.read()
        try:
            return await run_once(
                agent,
                prompt,
                output=config.output,
                provider=active_provider_name(),
                model=provider.model,
                working_directory=config.working_directory,
                compaction=compaction.strategy_name,
                tools_allowed=config.allow_tools,
            )
        finally:
            await mcp_manager.close()

    output.write(
        format_startup_message(
            provider=active_provider_name(),
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
            provider=active_provider_name(),
            model=provider.model,
            working_directory=config.working_directory,
            compaction=compaction.strategy_name,
            tools=[definition.name for definition in registry.definitions()],
            tui=TUI_NAME,
        ),
        mcp=mcp_manager,
        model_catalog=model_catalog,
        select_model=select_model,
        current_model_key=active_model_key,
        model_picker=pick_model,
        project_context=project_context,
    )
    try:
        await InteractiveRunner(input_adapter, output, OperationController()).run(
            agent, commands, command_context
        )
    finally:
        await mcp_manager.close()
    return 0
