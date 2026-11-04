import asyncio
import json
import threading
from pathlib import Path

import pytest

from harness.api.types import ProviderResponse, TextBlock, ToolCall, UserMessage
from harness.commands import CommandContext, build_default_commands
from harness.memory import MemoryConfig, MemoryManager, MemoryStore, RecordExperience
from harness.memory.transactions import settle_write
from harness.provider import MockProvider
from harness.subagents import SubagentManager
from harness.tools.base import ToolContext

TOOLS = Path(__file__).parents[1] / "tools.json"


def done(text="Finished"):
    return ProviderResponse([TextBlock(text)], "end_turn")


@pytest.mark.parametrize(
    "enabled,allow_tools,write_policy",
    [
        (None, False, "inherit"),
        (False, True, "inherit"),
        (True, False, "inherit"),
        (True, True, "inherit"),
        (True, True, "deny"),
    ],
)
async def test_main_root_only_memory_permissions_and_noninteractive_wrapper(
    tmp_path, monkeypatch, capsys, enabled, allow_tools, write_policy
):
    import harness.main as main_module

    (tmp_path / "tools.json").write_text(TOOLS.read_text())
    (tmp_path / "subagents.json").write_text(
        json.dumps(
            {
                "profiles": {
                    "worker": {
                        "description": "Worker",
                        "system_prompt": "Child role",
                        "permission_mode": "inherit",
                        "tools": [],
                    }
                }
            }
        )
    )
    if enabled is not None:
        (tmp_path / "memory.json").write_text(
            json.dumps({"enabled": enabled, "write_policy": write_policy})
        )
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    child = MockProvider([done("Private child claim")])
    calls = []

    async def respond(messages, definitions):
        calls.append((messages, definitions))
        index = len(calls)
        if not enabled:
            return done()
        if index == 1:
            return ProviderResponse(
                [ToolCall("delegate", "delegate_worker", {"task": "Bounded work"})], "tool_use"
            )
        if index == 2:
            outcome = json.loads(messages[-1].content[0].content)
            return ProviderResponse(
                [
                    ToolCall(
                        "record",
                        "record_experience",
                        {
                            "run_ids": [outcome["run_id"]],
                            "assessment": {"summary": "Useful finding"},
                            "applicability": "Similar work",
                            "tags": ["finding"],
                        },
                    )
                ],
                "tool_use",
            )
        if index == 3:
            assert messages[-1].content[0].is_error == (not allow_tools or write_policy == "deny")
            return ProviderResponse(
                [ToolCall("recall", "recall_experience", {"query": "finding"})], "tool_use"
            )
        assert messages[-1].kind == "memory_snapshot"
        return done()

    root = MockProvider(responder=respond)
    root.system = "Stable root system"
    providers = iter([root, child])
    monkeypatch.setattr(main_module, "build_provider", lambda *_args: next(providers))
    args = [
        "--working-dir",
        str(tmp_path),
        "--prompt",
        "Work",
        "--output",
        "json",
        "--harness-name",
        "memory-test",
    ]
    if allow_tools:
        args.append("--allow-tools")
    assert await main_module.main(args) == 0
    outer = json.loads(capsys.readouterr().out)
    assert outer["answer"] == "Finished" and outer["tools_allowed"] == allow_tools
    assert root.system == "Stable root system"
    root_names = {item.name for item in calls[0][1]}
    assert ("recall_experience" in root_names) == bool(enabled)
    assert ("record_experience" in root_names) == bool(enabled)
    if enabled:
        child_names = {item.name for item in child.calls[0][1]}
        assert "record_experience" not in child_names and "recall_experience" not in child_names
        assert "memory" not in child.system
    paths = list(home.rglob("records.json")) if home.exists() else []
    assert bool(paths) == bool(enabled and allow_tools and write_policy == "inherit")
    if paths:
        assert "Private child claim" not in paths[0].read_text()
    elif not enabled:
        assert not home.exists()


async def test_cancelled_write_waits_for_transaction_to_settle(tmp_path, monkeypatch):
    memory = MemoryManager(
        MemoryConfig(enabled=True),
        MemoryStore(tmp_path / "data", tmp_path),
        SubagentManager(lambda _profile: None, {}),
    )
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def record(_arguments):
        started.set()
        assert release.wait(2)
        completed.set()
        return {"status": "recorded", "id": "id"}

    monkeypatch.setattr(memory, "record", record)
    task = asyncio.create_task(RecordExperience(memory).execute(ToolContext(tmp_path), {}))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # Repeated interruption still cannot orphan the writer.
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()


async def test_cancelled_forget_clears_snapshot_after_confirmed_commit(tmp_path, monkeypatch):
    memory = MemoryManager(
        MemoryConfig(enabled=True),
        MemoryStore(tmp_path / "data", tmp_path),
        SubagentManager(lambda _profile: None, {}),
    )
    started = threading.Event()
    release = threading.Event()

    def forget(_record_id):
        started.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr(memory, "forget", forget)
    skill = UserMessage("FULL SKILL", preserve_from_compaction=True, kind="skill_activation")
    agent = type(
        "Conversation", (), {"messages": [skill, UserMessage("old", kind="memory_snapshot")]}
    )()
    context = CommandContext(agent=agent, provider=MockProvider(), tools=None, memory=memory)
    action = build_default_commands().dispatch("/memory forget id", context).action
    task = asyncio.create_task(action())
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.messages == [skill]


async def test_cancelled_worker_propagates_without_spinning():
    def cancelled_operation():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(settle_write(cancelled_operation), 2)
