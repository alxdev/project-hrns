from __future__ import annotations

import asyncio
import codecs
import os
import signal
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from harness.api.types import ToolResult

from .base import ToolLogSink


class _ProcessOptions(TypedDict):
    cwd: Path
    env: Mapping[str, str] | None
    stdin: int
    stdout: int
    stderr: int
    start_new_session: bool


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    exit_code: int

    def as_tool_result(self) -> ToolResult:
        output = self.stdout
        if self.exit_code:
            diagnostics = self.stderr.strip()
            if diagnostics:
                output = f"{output}\nDiagnostics:\n{diagnostics[-4000:]}".strip()
            output = f"{output}\nexit code: {self.exit_code}".strip()
        return ToolResult("", output, is_error=self.exit_code != 0)


async def run_process(
    command: str | Sequence[str],
    *,
    cwd: Path,
    tool_name: str,
    input_data: bytes | None = None,
    env: Mapping[str, str] | None = None,
    emit_log: ToolLogSink | None = None,
) -> ProcessResult:
    """Run shell text or an argument vector with shared I/O and cancellation.

    Each POSIX command owns a process group so cancellation also stops its
    descendants. Stderr is streamed to the caller without model requests.
    """
    options: _ProcessOptions = {
        "cwd": cwd,
        "env": env,
        "stdin": asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "start_new_session": os.name == "posix",
    }
    if isinstance(command, str):
        process = await asyncio.create_subprocess_shell(command, **options)
    else:
        process = await asyncio.create_subprocess_exec(*command, **options)
    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(process.stdout.read())
    stderr_task = asyncio.create_task(_read_stderr(process.stderr, tool_name, emit_log))
    stdin_task = asyncio.create_task(_write_stdin(process, input_data))
    try:
        await asyncio.gather(stdout_task, stderr_task, stdin_task)
        await process.wait()
    except BaseException:
        try:
            await _stop_process(process)
        finally:
            for task in (stdout_task, stderr_task, stdin_task):
                task.cancel()
            await asyncio.gather(stdout_task, stderr_task, stdin_task, return_exceptions=True)
        raise
    assert process.returncode is not None
    return ProcessResult(
        stdout_task.result().decode("utf-8", errors="replace"),
        stderr_task.result().decode("utf-8", errors="replace"),
        process.returncode,
    )


async def _write_stdin(process: asyncio.subprocess.Process, data: bytes | None) -> None:
    if process.stdin is None:
        return
    try:
        process.stdin.write(data or b"")
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()


async def _read_stderr(
    stream: asyncio.StreamReader, tool_name: str, emit_log: ToolLogSink | None
) -> bytes:
    chunks: list[bytes] = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while chunk := await stream.read(4096):
        chunks.append(chunk)
        text = decoder.decode(chunk)
        if text and emit_log is not None:
            await emit_log(tool_name, text, "stderr")
    tail = decoder.decode(b"", final=True)
    if tail and emit_log is not None:
        await emit_log(tool_name, tail, "stderr")
    return b"".join(chunks)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    elif process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except TimeoutError:
        pass
    finally:
        # The parent may exit while descendants hold pipes or ignore SIGTERM.
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
