"""Real terminal parsing and runner coverage for automatic paste handling."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from harness.api.types import ToolDefinition
from harness.commands import CommandContext, CommandRegistry, CommandResult
from harness.runtime.operations import OperationController
from harness.ui.interactive import InteractiveRunner
from harness.ui.prompt_toolkit_rich.input import PromptToolkitInput


@pytest.fixture(autouse=True, params=["dumb", "xterm-256color"])
def terminal_mode(request, monkeypatch):
    # prompt-toolkit uses distinct application objects for these paths.
    monkeypatch.setenv("TERM", request.param)


async def insert(pipe, ui, text, expected):
    changed = asyncio.Event()

    def check(buffer):
        if buffer.text == expected:
            changed.set()

    ui.session.default_buffer.on_text_changed += check
    try:
        pipe.send_text(text)
        await asyncio.wait_for(changed.wait(), 2)
    finally:
        ui.session.default_buffer.on_text_changed -= check


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"])
async def test_bracketed_multiline_paste_is_one_flat_input(tmp_path, line_ending):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        ui = PromptToolkitInput(tmp_path / "history")
        pending = asyncio.create_task(ui.read())
        body = line_ending.join(["First", "", "/clear", "Last", ""])
        expected = "before First  /clear Last after"
        await insert(pipe, ui, f"before \x1b[200~{body}\x1b[201~after", expected)
        await asyncio.sleep(0.12)
        assert not pending.done()
        pipe.send_text("\r")
        assert await asyncio.wait_for(pending, 1) == expected
        assert ui.last_input_was_multiline_paste
        assert ui.session.bottom_toolbar is None
        assert not ui.session.multiline


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("trailing", [False, True])
async def test_raw_multiline_paste_collects_burst_and_waits_for_enter(
    tmp_path, line_ending, trailing
):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        ui = PromptToolkitInput(tmp_path / "history")
        pending = asyncio.create_task(ui.read())
        lines = ["Lesson: pass precise task.", "", "Use available profile.", "Verify response."]
        if trailing:
            lines.append("")
        expected = " ".join(lines)
        await insert(pipe, ui, line_ending.join(lines), expected)
        await asyncio.sleep(0.12)
        assert not pending.done()
        pipe.send_text("\r")
        assert await asyncio.wait_for(pending, 1) == expected
        assert ui.last_input_was_multiline_paste


async def test_raw_paste_chunked_inside_grace_and_split_crlf(tmp_path):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        ui = PromptToolkitInput(tmp_path / "history")
        pending = asyncio.create_task(ui.read())
        await insert(pipe, ui, "First", "First")
        pipe.send_text("\r")
        await asyncio.sleep(0.02)
        await insert(pipe, ui, "\n\r\nSecond", "First  Second")
        pipe.send_text("\n")
        await asyncio.sleep(0.02)
        await insert(pipe, ui, "Third\r\n", "First  Second Third ")
        await asyncio.sleep(0.12)
        assert not pending.done()
        pipe.send_text("\r")
        assert await asyncio.wait_for(pending, 1) == "First  Second Third "


async def test_normal_enter_is_responsive_and_resets_paste_provenance(tmp_path):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        ui = PromptToolkitInput(tmp_path / "history")
        for payload, expected, pasted in [
            ("\x1b[200~First\nSecond\x1b[201~\r", "First Second", True),
            ("Ordinary input\r", "Ordinary input", False),
            ("\x1b[200~/memory list\x1b[201~\r", "/memory list", False),
        ]:
            pending = asyncio.create_task(ui.read())
            started = asyncio.get_running_loop().time()
            pipe.send_text(payload)
            assert await asyncio.wait_for(pending, 0.8) == expected
            assert asyncio.get_running_loop().time() - started < 0.8
            assert ui.last_input_was_multiline_paste is pasted


async def test_cancelled_pending_enter_cannot_submit_approval_or_secret(tmp_path):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        ui = PromptToolkitInput(tmp_path / "history")
        pending = asyncio.create_task(ui.read())
        await insert(pipe, ui, "Unsent", "Unsent")
        pipe.send_text("\r")
        await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert ui._pending_enter is None and ui._burst_timer is None
        assert ui._input_processor is None
        approval = asyncio.create_task(ui.approve(ToolDefinition("example", "Example", {}), {}))
        await asyncio.sleep(0.12)
        assert not approval.done()
        pipe.send_text("y\r")
        assert await asyncio.wait_for(approval, 1)
        secret = asyncio.create_task(ui.prompt_secret("FIXTURE_SECRET"))
        pipe.send_text("fixture-only\r")
        assert await asyncio.wait_for(secret, 1) == "fixture-only"


@pytest.mark.parametrize("submit", ["\r", "\x1b\r"])
async def test_multiline_slash_paste_cannot_turn_into_command_through_history(tmp_path, submit):
    history_path = tmp_path / "history"
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        ui = PromptToolkitInput(history_path)
        pending = asyncio.create_task(ui.read())
        pipe.send_text("Previous ordinary prompt\r")
        assert await asyncio.wait_for(pending, 1) == "Previous ordinary prompt"
        pending = asyncio.create_task(ui.read())
        pipe.send_text("\x1b[200~/clear\nExplain this\x1b[201~" + submit)
        assert await asyncio.wait_for(pending, 1) == "/clear Explain this"
        pending = asyncio.create_task(ui.read())
        pipe.send_text("\x1b[A\r")  # Up + Enter recalls only the ordinary prompt.
        assert await asyncio.wait_for(pending, 1) == "Previous ordinary prompt"
        assert not ui.last_input_was_multiline_paste
    assert "/clear" not in history_path.read_text()
    assert "Previous ordinary prompt" in history_path.read_text()


@pytest.mark.parametrize("bracketed", [False, True])
async def test_flattened_multiline_slash_paste_is_one_agent_prompt(tmp_path, bracketed):
    received = []
    invoked = []
    sent = asyncio.Event()
    command_called = asyncio.Event()

    class Agent:
        async def send(self, prompt):
            received.append(prompt)
            sent.set()

    class Output:
        @asynccontextmanager
        async def thinking(self):
            yield

        def write(self, _text):
            pass

        def error(self, error):
            pytest.fail(error)

    def clear(_args, _context):
        invoked.append("clear")
        command_called.set()
        return CommandResult()

    commands = CommandRegistry()
    commands.register("clear", "Fixture command", clear)
    commands.register("quit", "Quit", lambda *_args: CommandResult(should_exit=True))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        ui = PromptToolkitInput(tmp_path / "history")
        agent = Agent()
        runner = InteractiveRunner(ui, Output(), OperationController())
        pending = asyncio.create_task(
            runner.run(agent, commands, CommandContext(agent, None, None))
        )
        text = "/clear\n\n/clear\nExplain this text\n"
        payload = f"\x1b[200~{text}\x1b[201~" if bracketed else text
        await insert(pipe, ui, payload, "/clear  /clear Explain this text ")
        await asyncio.sleep(0.12)
        assert not received and not invoked
        pipe.send_text("\r")
        await asyncio.wait_for(sent.wait(), 2)
        pipe.send_text("\x1b[200~/clear\x1b[201~\r")
        await asyncio.wait_for(command_called.wait(), 2)
        pipe.send_text("/quit\r")
        await asyncio.wait_for(pending, 2)
    assert received == ["/clear  /clear Explain this text "]
    assert invoked == ["clear"]
