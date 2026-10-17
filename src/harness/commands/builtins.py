from __future__ import annotations

import shlex

from harness.commands.registry import CommandContext, CommandRegistry, CommandResult
from harness.mcp import MCPServerConfig, normalize_server_name, server_name_from_url


def build_default_commands() -> CommandRegistry:
    registry = CommandRegistry()
    registry.register("help", "Show available commands.", _help)
    registry.register("status", "Show current runtime configuration.", _status)
    registry.register("model", "Show or change the current model.", _model, "/model [name]")
    registry.register("clear", "Clear the conversation history.", _clear)
    registry.register("tools", "List registered tools.", _tools)
    registry.register("agents", "List agents, descriptions, and available tools.", _agents)
    registry.register(
        "mcp",
        "Register MCP tools for one subagent or all subagents.",
        _mcp,
        (
            "/mcp add <url> (--agent <name>|--global) [--name <alias>] "
            "| /mcp list | /mcp remove <name>"
        ),
    )
    registry.register(
        "compact", "Compact conversation history.", _compact, "/compact [none|sliding|summarize]"
    )
    registry.register(
        "verbose", "Show or change compaction reporting.", _verbose, "/verbose [on|off]"
    )
    registry.register("exit", "Exit the harness.", _exit)
    registry.register("quit", "Exit the harness.", _exit)
    return registry


def _help(_args: str, context: CommandContext) -> CommandResult:
    if context.commands is None:
        return CommandResult("command registry unavailable")
    lines = [
        f"{command.usage:<18} {command.description}" for command in context.commands.commands()
    ]
    return CommandResult("\n".join(lines))


def _model(args: str, context: CommandContext) -> CommandResult:
    if not args:
        return CommandResult(f"current model: {context.provider.model}")
    set_model = getattr(context.provider, "set_model", None)
    if set_model is None:
        return CommandResult("current provider does not support changing models")
    set_model(args)
    return CommandResult(f"model: {context.provider.model}")


def _status(_args: str, context: CommandContext) -> CommandResult:
    if context.runtime_info is None:
        return CommandResult("runtime configuration unavailable")
    return CommandResult(context.runtime_info())


def _clear(_args: str, context: CommandContext) -> CommandResult:
    context.agent.messages.clear()
    if context.transcript is not None:
        context.transcript.clear()
    return CommandResult("conversation cleared")


def _tools(_args: str, context: CommandContext) -> CommandResult:
    sections: list[str] = []
    root_definitions = context.tools.definitions()
    root_entries = [f"  {item.name} — {item.description}" for item in root_definitions]
    if context.mcp is not None:
        root_entries.extend(
            f"  MCP {server}: {status}" for server, status in context.mcp.discovery_status("root")
        )
    sections.append("root tools:\n\n" + "\n\n".join(root_entries or ["  none"]))

    if context.subagents is not None:
        for name in sorted(context.subagents.profiles):
            tools = list(context.subagents.tools_for_profile(name))
            if context.mcp is not None:
                known = {tool.definition.name for tool in tools}
                tools.extend(
                    tool
                    for tool in context.mcp.cached_tools_for(name)
                    if tool.definition.name not in known
                )
            entries = [
                f"  {tool.definition.name} — {tool.definition.description}" for tool in tools
            ]
            if context.mcp is not None:
                entries.extend(
                    f"  MCP {server}: {status}"
                    for server, status in context.mcp.discovery_status(name)
                )
            sections.append(f"agent_{name} tools:\n\n" + "\n\n".join(entries or ["  none"]))
    return CommandResult("\n\n".join(sections))


def _agents(_args: str, context: CommandContext) -> CommandResult:
    if context.subagents is None:
        return CommandResult("subagents are unavailable")
    extra_tools = context.mcp.cached_tools_for if context.mcp is not None else None
    root_tool_names = (
        ", ".join(definition.name for definition in context.tools.definitions()) or "none"
    )
    root_entry = (
        "  agent_root — Coordinates requests and delegates focused work to specialized agents. "
        f"— tools: {root_tool_names}"
    )
    return CommandResult(context.subagents.describe(extra_tools, [root_entry]))


def _mcp(args: str, context: CommandContext) -> CommandResult:
    if context.mcp is None:
        return CommandResult("MCP support is unavailable")
    try:
        parts = shlex.split(args)
    except ValueError as exc:
        return CommandResult(f"invalid MCP command: {exc}")
    if not parts or parts[0] == "list":
        return CommandResult(context.mcp.describe())
    if parts[0] == "remove" and len(parts) == 2:
        try:
            server_name = normalize_server_name(parts[1])
            removed = context.mcp.unregister(server_name)
        except ValueError as exc:
            return CommandResult(str(exc))
        for definition in context.tools.definitions():
            if definition.name.startswith(f"mcp_{server_name}_"):
                context.tools.unregister(definition.name)
        if context.subagents is not None:
            context.subagents.unregister_tools(f"mcp_{server_name}_")
        return CommandResult(
            f"removed {removed} MCP registration(s) named {parts[1]}"
            if removed
            else f"no MCP registration found: {parts[1]}"
        )
    if parts[0] != "add" or len(parts) < 2:
        return CommandResult("usage: /mcp add <url> (--agent <name>|--global) [--name <alias>]")

    url = parts[1]
    options: dict[str, str | bool] = {}
    index = 2
    while index < len(parts):
        option = parts[index]
        if option == "--global":
            options[option] = True
            index += 1
        elif option in {"--agent", "--name"} and index + 1 < len(parts):
            options[option] = parts[index + 1]
            index += 2
        else:
            return CommandResult(f"unknown or incomplete MCP option: {option}")
    if ("--global" in options) == ("--agent" in options):
        return CommandResult("choose exactly one scope: --agent <name> or --global")
    try:
        name = normalize_server_name(str(options.get("--name") or server_name_from_url(url)))
        if "--agent" in options:
            profile = str(options["--agent"])
            if profile not in context.subagents.profiles:
                available = ", ".join(sorted(context.subagents.profiles))
                return CommandResult(f"unknown subagent '{profile}'; available: {available}")
            server = MCPServerConfig(name, url, "agent", profile)
            scope = f"subagent {profile}"
        else:
            server = MCPServerConfig(name, url, "global")
            scope = "all agents"
        context.mcp.register(server)
    except ValueError as exc:
        return CommandResult(str(exc))
    return CommandResult(f"registered {name} MCP server for {scope}; tools load on first use")


def _compact(args: str, context: CommandContext) -> CommandResult:
    if context.compaction is None:
        return CommandResult("compaction is unavailable")
    mode = args or None
    if mode is not None and mode.lower() not in {"none", "sliding", "summarize"}:
        return CommandResult("usage: /compact [none|sliding|summarize]")

    async def action() -> str:
        updated, report = await context.compaction.run(context.agent.messages, mode)
        context.agent.messages = updated
        selection = "active strategy" if mode is None else "one-time strategy"
        status = "history changed" if report.changed else "history unchanged"
        return (
            "compaction\n"
            f"  strategy: {report.strategy}\n"
            f"  selection: {selection}\n"
            f"  messages: {report.before_count} -> {report.after_count}\n"
            f"  status: {status}"
        )

    return CommandResult(action=action)


def _verbose(args: str, context: CommandContext) -> CommandResult:
    if context.compaction is None:
        return CommandResult("compaction is unavailable")
    if not args:
        return CommandResult(f"verbose compaction: {'on' if context.compaction.verbose else 'off'}")
    if args.lower() not in {"on", "off"}:
        return CommandResult("usage: /verbose [on|off]")
    context.compaction.verbose = args.lower() == "on"
    return CommandResult(f"verbose compaction: {'on' if context.compaction.verbose else 'off'}")


def _exit(_args: str, _context: CommandContext) -> CommandResult:
    return CommandResult("goodbye", should_exit=True)
