import asyncio
import json
from datetime import datetime

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolResult, UserMessage
from harness.compaction import CompactionManager
from harness.compaction.strategies import SlidingWindow, Summarize
from harness.memory import (
    MemoryConfig,
    MemoryManager,
    MemorySnapshot,
    MemoryStore,
    RecallExperience,
)
from harness.permissions import AlwaysAllow, AlwaysDeny
from harness.provider import MockProvider
from harness.subagents import SubagentManager
from harness.subagents.models import SubagentRecord, SubagentStatus
from harness.tools.base import ToolContext
from harness.tools.registry import ToolRegistry


def setup(tmp_path):
    subagents = SubagentManager(lambda _profile: None, {})
    run = SubagentRecord(
        "run",
        "worker",
        "Private task",
        status=SubagentStatus.COMPLETED,
        completed_at=datetime.now(),
        finalized=True,
        working_directory=tmp_path,
    )
    subagents._records.append(run)
    memory = MemoryManager(
        MemoryConfig(enabled=True), MemoryStore(tmp_path / "data", tmp_path), subagents
    )
    for tag in ("alpha", "beta"):
        memory.record(
            {
                "run_ids": [run.id],
                "assessment": {"summary": tag},
                "tags": [tag],
                "applicability": tag,
            }
        )
    recall = RecallExperience(memory)
    registry = ToolRegistry([recall])
    return memory, recall, registry


def respond(calls):
    return ProviderResponse(
        [
            ToolCall(str(index), "recall_experience", {"tags": [tag]})
            for index, tag in enumerate(calls)
        ],
        "tool_use",
    )


@pytest.mark.parametrize("mode", ["sliding", "summarize"])
async def test_repeated_recall_keeps_one_snapshot_and_full_skill_context(tmp_path, mode):
    _memory, recall, registry = setup(tmp_path)
    provider = MockProvider(
        [
            respond(["alpha"]),
            respond(["alpha", "beta"]),
            ProviderResponse([TextBlock("done")], "end_turn"),
        ]
    )
    summarizer = MockProvider(responder=lambda _messages, _tools: summary())
    strategy = (
        SlidingWindow(2) if mode == "sliding" else Summarize(summarizer, threshold=2, keep_recent=1)
    )
    agent = Agent(
        provider,
        registry,
        AlwaysAllow(),
        ToolContext(tmp_path),
        compaction=CompactionManager(provider, strategy),
        after_tool_batch=MemorySnapshot(recall, registry),
    )
    skill = UserMessage(
        "FULL SKILL CONTENT", kind="skill_activation", preserve_from_compaction=True
    )
    agent.messages = [skill]
    assert await agent.send("Find memories") == "done"
    for _ in range(3):
        agent.messages = await strategy.compact(agent.messages)
    snapshots = [message for message in agent.messages if message.kind == "memory_snapshot"]
    assert len(snapshots) == 1 and '"summary":"beta"' in snapshots[0].content[0].text
    assert '"summary":"alpha"' not in snapshots[0].content[0].text
    assert any(message is skill for message in agent.messages)
    for messages, _tools in summarizer.calls:
        assert "FULL SKILL CONTENT" not in messages[0].content[0].text
        assert "[Experience memory snapshot]" not in messages[0].content[0].text


async def summary():
    return ProviderResponse([TextBlock("Earlier work")], "end_turn")


@pytest.mark.parametrize("phase", ["same_batch", "next_request"])
async def test_cancelled_recall_restores_exact_context_without_pending_reinjection(tmp_path, phase):
    _memory, recall, registry = setup(tmp_path)
    waiting = asyncio.Event()
    count = 0

    async def response(_messages, _definitions):
        nonlocal count
        count += 1
        if count == 1:
            result = respond(["beta"])
            if phase == "same_batch":
                result = ProviderResponse(
                    [*result.content, ToolCall("cancel", "wait", {})], "tool_use"
                )
            return result
        waiting.set()
        await asyncio.Event().wait()

    class Events:
        async def emit(self, event):
            if getattr(event, "name", "") == "wait":
                waiting.set()
                await asyncio.Event().wait()

    agent = Agent(
        MockProvider(responder=response),
        registry,
        AlwaysAllow(),
        ToolContext(tmp_path),
        events=Events(),
        after_tool_batch=MemorySnapshot(recall, registry),
    )
    skill = UserMessage("FULL SKILL", kind="skill_activation", preserve_from_compaction=True)
    old = UserMessage("OLD MEMORY", kind="memory_snapshot", preserve_from_compaction=True)
    original = [skill, old]
    agent.messages = list(original)
    task = asyncio.create_task(agent.send("Recall"))
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.messages == original and agent.messages[0] is skill and agent.messages[1] is old
    agent.provider = MockProvider([ProviderResponse([TextBlock("new turn")], "end_turn")])
    await agent.send("Continue")
    assert [message for message in agent.messages if message.kind == "memory_snapshot"] == [old]


async def test_denied_recall_keeps_snapshot_and_empty_success_replaces_it(tmp_path):
    _memory, recall, registry = setup(tmp_path)
    agent = Agent(
        MockProvider([respond(["unknown"]), ProviderResponse([TextBlock("done")], "end_turn")]),
        registry,
        AlwaysDeny(),
        ToolContext(tmp_path),
        after_tool_batch=MemorySnapshot(recall, registry),
    )
    old = UserMessage("OLD", kind="memory_snapshot", preserve_from_compaction=True)
    agent.messages = [old]
    await agent.send("Denied")
    assert old in agent.messages
    agent.permissions = AlwaysAllow()
    agent.provider = MockProvider(
        [respond(["unknown"]), ProviderResponse([TextBlock("done")], "end_turn")]
    )
    await agent.send("Empty")
    snapshots = [message for message in agent.messages if message.kind == "memory_snapshot"]
    assert len(snapshots) == 1 and old not in agent.messages
    assert json.loads(snapshots[0].content[0].text.split("\n", 1)[1])["records"] == []
    # An unrelated tool's similar JSON never becomes protected memory.
    hook = MemorySnapshot(recall, registry)
    assert (
        hook(
            agent.messages,
            [(ToolCall("other", "unrelated", {}), ToolResult("other", '{"status":"recalled"}'))],
        )
        is agent.messages
    )
