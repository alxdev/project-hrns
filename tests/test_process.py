import asyncio
import os
import re
import shlex
import signal
import subprocess
import sys

import pytest

from harness.tools.process import run_process


async def test_stderr_streams_before_process_completion(tmp_path):
    started = asyncio.Event()
    logs = []

    async def emit_log(name, text, stream):
        logs.append((name, text, stream))
        if "ready" in "".join(item[1] for item in logs):
            started.set()

    source = (
        "import pathlib,sys,time\n"
        'print("ready", file=sys.stderr, flush=True)\n'
        'while not pathlib.Path("continue").exists(): time.sleep(0.01)\n'
        "print(sys.stdin.read())\n"
    )
    task = asyncio.create_task(
        run_process(
            [sys.executable, "-c", source],
            cwd=tmp_path,
            tool_name="generated_tool",
            input_data=b'{"message":"ok"}',
            emit_log=emit_log,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not task.done()
        (tmp_path / "continue").touch()
        result = await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert result.stdout.strip() == '{"message":"ok"}'
    assert result.stderr.strip() == "ready"
    assert all(name == "generated_tool" and stream == "stderr" for name, _, stream in logs)


async def test_command_errors_return_shared_diagnostics(tmp_path):
    result = await run_process(
        [
            sys.executable,
            "-c",
            'import sys; print("output"); print("problem",file=sys.stderr); sys.exit(3)',
        ],
        cwd=tmp_path,
        tool_name="generated_tool",
    )

    tool_result = result.as_tool_result()

    assert tool_result.is_error
    assert "output" in tool_result.content
    assert "Diagnostics:\nproblem" in tool_result.content
    assert "exit code: 3" in tool_result.content


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
@pytest.mark.parametrize("shell", [False, True])
async def test_cancellation_stops_descendants_even_when_they_ignore_sigterm(tmp_path, shell):
    started = asyncio.Event()
    child_pid = None
    stderr = ""

    async def emit_log(_name, text, _stream):
        nonlocal child_pid, stderr
        stderr += text
        match = re.search(r"CHILD=(\d+)", stderr)
        if match:
            child_pid = int(match.group(1))
            started.set()

    child_source = (
        "import os,signal,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        'print("CHILD="+str(os.getpid()),file=sys.stderr,flush=True); time.sleep(30)'
    )
    parent_source = (
        f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child_source!r}]); '
        "time.sleep(30)"
    )
    argv = [sys.executable, "-c", parent_source]
    command = shlex.join(argv) if shell else argv
    task = asyncio.create_task(
        run_process(command, cwd=tmp_path, tool_name="example", emit_log=emit_log)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert child_pid is not None
        # A killed orphan may briefly exist as a zombie until the OS reaps it.
        for _ in range(50):
            status = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True
            ).stdout.strip()
            if not status or status.startswith("Z"):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail(f"Descendant {child_pid} remained running: {status}")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
