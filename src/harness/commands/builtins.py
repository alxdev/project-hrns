from __future__ import annotations

from harness.commands.registry import CommandContext, CommandRegistry, CommandResult


def build_default_commands() -> CommandRegistry:
    registry = CommandRegistry()
    registry.register("help", "Show available commands.", _help)
    registry.register("model", "Show or change the current model.", _model, "/model [name]")
    registry.register("clear", "Clear the conversation history.", _clear)
    registry.register("tools", "List registered tools.", _tools)
    registry.register("compact", "Compact conversation history.", _compact, "/compact [none|sliding|summarize]")
    registry.register("verbose", "Show or change compaction reporting.", _verbose, "/verbose [on|off]")
    registry.register("exit", "Exit the harness.", _exit)
    registry.register("quit", "Exit the harness.", _exit)
    return registry


def _help(_args: str, context: CommandContext) -> CommandResult:
    if context.commands is None:
        return CommandResult("command registry unavailable")
    lines = [f"{command.usage:<18} {command.description}" for command in context.commands.commands()]
    return CommandResult("\n".join(lines))


def _model(args: str, context: CommandContext) -> CommandResult:
    if not args:
        return CommandResult(f"current model: {context.provider.model}")
    set_model = getattr(context.provider, "set_model", None)
    if set_model is None:
        return CommandResult("current provider does not support changing models")
    set_model(args)
    return CommandResult(f"model: {context.provider.model}")


def _clear(_args: str, context: CommandContext) -> CommandResult:
    context.agent.messages.clear()
    return CommandResult("conversation cleared")


def _tools(_args: str, context: CommandContext) -> CommandResult:
    names = [definition.name for definition in context.tools.definitions()]
    return CommandResult("\n".join(names) if names else "no tools registered")


def _compact(args: str, context: CommandContext) -> CommandResult:
    if context.compaction is None:
        return CommandResult("compaction is unavailable")
    mode = args or None
    if mode is not None and mode.lower() not in {"none", "sliding", "summarize"}:
        return CommandResult("usage: /compact [none|sliding|summarize]")

    async def action() -> str:
        updated, report = await context.compaction.run(context.agent.messages, mode)
        context.agent.messages = updated
        if not context.compaction.verbose:
            return "compaction complete"
        return f"compaction: {report.before_count} messages -> {report.after_count} messages"

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
