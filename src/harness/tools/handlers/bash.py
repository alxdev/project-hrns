from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from harness.api.types import ToolResult
from harness.tools.base import ToolContext


async def execute(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if os.name == "posix":
        process = await asyncio.create_subprocess_shell(
            arguments["command"],
            cwd=context.working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    else:
        process = await asyncio.create_subprocess_shell(
            arguments["command"],
            cwd=context.working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    try:
        output, _ = await process.communicate()
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    text = output.decode(errors="replace")
    if process.returncode:
        text = f"{text}\nexit code: {process.returncode}".strip()
    return ToolResult("", text, is_error=process.returncode != 0)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        if process.returncode is not None:
            return
        process.terminate()
    try:
        if process.returncode is None:
            await asyncio.wait_for(process.wait(), timeout=1)
    except TimeoutError:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        await process.wait()
