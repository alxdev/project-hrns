import io
from pathlib import Path

from rich.console import Console

from harness.agent.loop import ToolLogEvent
from harness.api.types import ToolDefinition
from harness.tools.base import ToolContext
from harness.tools.handlers.bash import execute as execute_bash
from harness.tools.loader import ConfiguredTool
from harness.transcript import Transcript, TranscriptEventSink
from harness.ui.prompt_toolkit_rich.output import RichOutput


async def test_literal_stderr_renders_without_markup_and_uses_actual_tool_name(tmp_path):
    terminal = io.StringIO()
    output = RichOutput(Console(file=terminal, width=100))
    transcript = Transcript()
    sink = TranscriptEventSink(transcript, output)
    logs = []

    async def emit_log(name, text, stream):
        logs.append((name, text, stream))
        await sink.emit(ToolLogEvent(name, text, stream))

    tool = ConfiguredTool(ToolDefinition("custom_shell", "Run", {"type": "object"}), execute_bash)
    result = await tool.execute(
        ToolContext(Path(tmp_path), emit_log=emit_log),
        {"command": "printf '[/unexpected] [bold]literal[/bold]\\n' >&2"},
    )

    assert not result.is_error
    assert "[/unexpected] [bold]literal[/bold]" in terminal.getvalue()
    assert logs and all(name == "custom_shell" and stream == "stderr" for name, _, stream in logs)
