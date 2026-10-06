from __future__ import annotations

from harness.commands.registry import CommandContext, CommandRegistry, CommandResult


def build_default_commands() -> CommandRegistry:
    registry = CommandRegistry()
    registry.register("help", "Show available commands.", _help)
    registry.register("model", "Show or change the current model.", _model, "/model [name]")
    registry.register("clear", "Clear the conversation history.", _clear)
    registry.register("tools", "List registered tools.", _tools)
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


def _exit(_args: str, _context: CommandContext) -> CommandResult:
    return CommandResult("goodbye", should_exit=True)
