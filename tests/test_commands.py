from harness.commands import CommandContext, build_default_commands
from harness.provider import MockProvider
from harness.tools.registry import ToolRegistry


class AgentStub:
    def __init__(self):
        self.messages = ["message"]


def context():
    return CommandContext(
        agent=AgentStub(),
        provider=MockProvider(model="initial-model"),
        tools=ToolRegistry(),
        commands=build_default_commands(),
    )


def test_help_and_unknown_commands_are_handled():
    commands = build_default_commands()
    command_context = context()

    help_result = commands.dispatch("/help", command_context)
    unknown_result = commands.dispatch("/missing", command_context)

    assert "/clear" in help_result.text
    assert "unknown command" in unknown_result.text


def test_model_and_clear_commands_mutate_only_their_context():
    commands = build_default_commands()
    command_context = context()

    model_result = commands.dispatch("/model changed-model", command_context)
    clear_result = commands.dispatch("/clear", command_context)

    assert model_result.text == "model: changed-model"
    assert command_context.provider.model == "changed-model"
    assert clear_result.text == "conversation cleared"
    assert command_context.agent.messages == []
