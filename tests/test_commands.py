from harness.commands import CommandContext, build_default_commands
from harness.project_context import ProjectContext
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
        runtime_info=lambda: "runtime details",
    )


def test_help_and_unknown_commands_are_handled():
    commands = build_default_commands()
    command_context = context()

    help_result = commands.dispatch("/help", command_context)
    unknown_result = commands.dispatch("/missing", command_context)

    assert "/clear" in help_result.text
    assert "/agents" in help_result.text
    assert "/subagents" not in help_result.text
    assert "unknown command" in unknown_result.text

    legacy_command = commands.dispatch("/subagents", command_context)
    assert legacy_command is not None and "unknown command" in legacy_command.text


def test_model_and_clear_commands_mutate_only_their_context():
    commands = build_default_commands()
    command_context = context()

    model_result = commands.dispatch("/model changed-model", command_context)
    clear_result = commands.dispatch("/clear", command_context)

    assert model_result.text == "model: changed-model"
    assert command_context.provider.model == "changed-model"
    assert clear_result.text == "conversation cleared"
    assert command_context.agent.messages == []


def test_status_displays_shared_runtime_configuration():
    commands = build_default_commands()
    command_context = context()

    result = commands.dispatch("/status", command_context)

    assert result.text == "runtime details"


def test_context_displays_loaded_root_project_context(tmp_path):
    command_context = context()
    command_context.project_context = ProjectContext(
        working_directory=tmp_path,
        root_path=tmp_path / "AGENTS.md",
        root_content="shared instructions",
    )

    result = command_context.commands.dispatch("/context", command_context)

    assert str(tmp_path / "AGENTS.md") in result.text
    assert "not found" not in result.text


def test_models_lists_and_selects_from_the_catalog():
    command_context = context()
    selected: list[str] = []
    command_context.model_catalog = type(
        "CatalogStub",
        (),
        {"describe": lambda _self, _current: "available models"},
    )()
    command_context.current_model_key = lambda: "openai/initial-model"
    command_context.select_model = lambda model: selected.append(model) or f"model: {model}"

    listed = command_context.commands.dispatch("/models", command_context)
    changed = command_context.commands.dispatch("/models openai/changed", command_context)

    assert listed.text == "available models"
    assert changed.text == "model: openai/changed"
    assert selected == ["openai/changed"]


def test_models_without_arguments_uses_the_interactive_picker():
    command_context = context()
    command_context.model_catalog = type(
        "CatalogStub",
        (),
        {"describe": lambda _self, _current: "available models"},
    )()

    async def picker():
        return "picked"

    command_context.model_picker = picker

    result = command_context.commands.dispatch("/models", command_context)

    assert result.text == ""
    assert result.action is picker
