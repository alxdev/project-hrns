import sys
from contextlib import nullcontext
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.plain_text import PlainTextOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.patch_stdout import StdoutProxy
from rich.text import Text

from harness.api.types import ToolDefinition
from harness.ui.minimal_tui.terminal import TerminalUI
from harness.ui.prompt_toolkit_rich import input as rich_input


@pytest.fixture(params=["minimal", "rich"])
def approval_ui(request, monkeypatch, tmp_path):
    if request.param == "minimal":
        ui = TerminalUI()

        def set_answer(callback):
            monkeypatch.setattr("builtins.input", callback)

    else:
        session = SimpleNamespace(prompt_async=AsyncMock())
        monkeypatch.setattr(rich_input, "PromptSession", lambda **kwargs: session)
        monkeypatch.setattr(rich_input, "patch_stdout", lambda **kwargs: nullcontext())
        ui = rich_input.PromptToolkitInput(tmp_path / "history")

        def set_answer(callback):
            session.prompt_async.side_effect = callback

    return ui, set_answer


@pytest.mark.parametrize(
    "answer,approved",
    [("y", True), (" YES ", True), ("", False), ("n", False), ("true", False), ("yep", False)],
)
async def test_approval_details_precede_short_question(approval_ui, capsys, answer, approved):
    ui, set_answer = approval_ui
    arguments = {"path": "résumé.txt", "options": {"overwrite": False}}
    tool = ToolDefinition("write_file", "Write [bold]literal[/bold] text to a file", {})

    def respond(prompt):
        details = capsys.readouterr().out
        assert tool.name in details
        assert tool.description in details
        assert "Arguments:" in details
        assert '"path": "résumé.txt"' in details
        assert '"options": {' in details
        assert '"overwrite": false' in details
        assert prompt == "Approve write_file? [y/N] "
        return answer

    set_answer(respond)
    assert await ui.approve(tool, arguments) is approved
    assert arguments == {"path": "résumé.txt", "options": {"overwrite": False}}


async def test_artifact_recovery_has_explicit_archive_prepare_question(approval_ui, capsys):
    ui, set_answer = approval_ui
    tool = ToolDefinition(
        "recreate_invalid_artifact",
        "Archive the invalid original and prepare a replacement",
        {},
    )
    arguments = {"name": "helper"}

    def respond(prompt):
        details = capsys.readouterr().out
        assert tool.description in details
        assert '"name": "helper"' in details
        assert prompt == "Archive invalid artifact and prepare replacement? [y/N] "
        return "yes"

    set_answer(respond)
    assert await ui.approve(tool, arguments)


async def test_regular_input_prompt_is_unchanged(approval_ui):
    ui, set_answer = approval_ui

    def respond(prompt):
        assert prompt == "❯ "
        return "hello"

    set_answer(respond)
    assert await ui.read() == "hello"


@pytest.mark.parametrize("is_terminal", [True, False], ids=["vt100", "non-tty"])
async def test_rich_approval_through_real_stdout_proxy(monkeypatch, tmp_path, is_terminal):
    stream = StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: is_terminal)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    output = (
        Vt100_Output(
            stream,
            get_size=lambda: Size(rows=24, columns=100),
            term="xterm-256color",
            default_color_depth=ColorDepth.DEPTH_8_BIT,
        )
        if is_terminal
        else PlainTextOutput(stream)
    )
    tool = ToolDefinition("[bold]write[/bold]", "Write [red]literal[/red] text", {})
    arguments = {"path": "résumé[blue].txt", "overwrite": False}

    async def respond(prompt):
        assert isinstance(sys.stdout, StdoutProxy)
        assert prompt == f"Approve {tool.name}? [y/N] "
        return "y"

    session = SimpleNamespace(prompt_async=AsyncMock(side_effect=respond))
    monkeypatch.setattr(rich_input, "PromptSession", lambda **kwargs: session)
    with create_app_session(input=DummyInput(), output=output):
        ui = rich_input.PromptToolkitInput(tmp_path / "history")
        assert await ui.approve(tool, arguments)

    # Exiting patch_stdout joins its real flush thread, so all output is available.
    rendered = stream.getvalue()
    if is_terminal:
        assert "\x1b[33m" in rendered
        assert "?[33m" not in rendered
    else:
        assert "\x1b" not in rendered
    plain = Text.from_ansi(rendered).plain
    assert f"Approval · {tool.name}" in plain
    assert tool.description in plain
    assert "Arguments:" in plain
    assert '"path": "résumé[blue].txt"' in plain
    assert '"overwrite": false' in plain
    session.prompt_async.assert_awaited_once()
