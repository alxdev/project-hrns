"""Delegates receive focused assignments, not automatic root request/history copies."""

import asyncio

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolResult
from harness.compaction import CompactionManager, SlidingWindow, Summarize
from harness.config import parse_config
from harness.main import build_provider
from harness.orchestration import ROOT_INSTRUCTIONS, root_system_prompt
from harness.permissions import AlwaysAllow
from harness.project_context import ProjectContext
from harness.provider import MockProvider
from harness.provider.base import ProviderResponseCompleted, ProviderTextDelta
from harness.request_context import render_handoff
from harness.subagents import DelegationTool, SubagentManager, SubagentProfile
from harness.tools.base import ToolContext
from harness.tools.registry import ToolRegistry


def done(text="Done"):
    return ProviderResponse([TextBlock(text)], "end_turn")


def call(task="Inspect"):
    return ProviderResponse([ToolCall("delegate", "delegate_worker", {"task": task})], "tool_use")


def text_context(messages):
    return "\n".join(
        block.text
        for message in messages
        for block in message.content
        if isinstance(block, TextBlock)
    )


def agent(tmp_path, provider, tools=(), **kwargs):
    return Agent(provider, ToolRegistry(tools), AlwaysAllow(), ToolContext(tmp_path), **kwargs)


def delegator(tmp_path, child_factory):
    manager = SubagentManager(
        child_factory, {"worker": SubagentProfile("worker", "Worker", "Worker")}
    )
    tool = DelegationTool("worker", "Worker")
    tool.bind(manager)
    return manager, tool


async def test_root_keeps_raw_requests_and_delegates_only_focused_tasks_to_fresh_children(tmp_path):
    children = []

    def factory(_profile):
        child = agent(tmp_path, MockProvider([done(), done()]), name="worker")
        assert child.messages == []
        assert not child.context.artifact_authoring_intents
        child.context.artifact_authoring_intents.add((tmp_path, tmp_path, "local-preparation"))
        children.append(child)
        return child

    manager, tool = delegator(tmp_path, factory)
    root_provider = MockProvider([call(), done(), call(), done()])
    root = agent(tmp_path, root_provider, [tool], name="root")
    origins = ["  Exact request\nincluding whitespace\n", "New user turn"]
    for origin in origins:
        await root.send(origin)
    calls = [entry for child in children for entry in child.provider.calls]
    for messages, _ in calls:
        assert len(messages) == 1
        assert messages[0].content[0].text == "Focused assigned task:\nInspect"
        assert all(origin not in text_context(messages) for origin in origins)
    assert origins[0] not in str(calls[1][0])
    assert all("Original user request" not in str(child.messages) for child in children)
    assert len(children) == 2
    assert children[0] is not children[1]
    assert (
        children[0].context.artifact_authoring_intents
        is not children[1].context.artifact_authoring_intents
    )
    assert manager.records()[0].prompt == "Inspect"
    assert root_provider.calls[0][1][0].input_schema["required"] == ["task"]
    assert all(origin in text_context(root.messages) for origin in origins)
    assert "Focused assigned task:" not in str(root.messages)


async def test_nested_children_receive_only_their_assigned_tasks(tmp_path):
    leaf = agent(tmp_path, MockProvider([done()]), name="leaf")
    _, nested = delegator(tmp_path, lambda _profile: leaf)
    child = agent(tmp_path, MockProvider([call("Nested task"), done()]), [nested], name="worker")
    _, tool = delegator(tmp_path, lambda _profile: child)
    root = agent(tmp_path, MockProvider([call("Parent task"), done()]), [tool], name="root")
    await root.send("Original task")
    assert len(leaf.provider.calls[0][0]) == 1
    assert leaf.provider.calls[0][0][0].content[0].text == "Focused assigned task:\nNested task"
    assert "Original task" not in str(child.provider.calls)
    assert "Original task" not in str(leaf.provider.calls)
    assert "Parent task" not in str(leaf.provider.calls)


async def test_concurrent_roots_isolate_focused_tasks_and_fresh_children(tmp_path):
    arrived = asyncio.Event()
    origins = []
    children = []

    async def respond(messages, _tools):
        origins.append(messages[0].content[0].text)
        if len(origins) == 2:
            arrived.set()
        await arrived.wait()
        return done()

    def factory(_profile):
        child = agent(tmp_path, MockProvider(responder=respond), name="worker")
        children.append(child)
        return child

    manager, tool = delegator(tmp_path, factory)
    roots = [
        agent(tmp_path, MockProvider([call(task), done()]), [tool], name="root")
        for task in ("First focused task", "Second focused task")
    ]
    await asyncio.wait_for(
        asyncio.gather(roots[0].send("First origin"), roots[1].send("Second origin")), 2
    )
    assert len(children) == 2
    assert set(origins) == {
        "Focused assigned task:\nFirst focused task",
        "Focused assigned task:\nSecond focused task",
    }
    assert manager.active() == []


@pytest.mark.parametrize("cancel", [False, True])
async def test_request_failure_and_cancellation_preserve_existing_history_rules(tmp_path, cancel):
    async def fail(_messages, _tools):
        assert _messages[0].content[0].text == "Origin"
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError("provider failed")

    root = agent(tmp_path, MockProvider(responder=fail), name="root")
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await root.send("Origin")
    if cancel:
        assert root.messages == []


@pytest.mark.parametrize("mode", ["sliding", "summarize"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_only_focused_assignment_survives_every_child_compaction(tmp_path, mode, streaming):
    class StreamingProvider:
        model = "streaming-test"

        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        async def send(self, messages, tools):
            raise AssertionError("Streaming test must use stream(), not send()")

        async def stream(self, messages, tools):
            self.calls.append((list(messages), list(tools)))
            response = self.responses.pop(0)
            for block in response.content:
                if isinstance(block, TextBlock):
                    yield ProviderTextDelta(block.text)
            yield ProviderResponseCompleted(response)

    summary_provider = MockProvider([done("Summary") for _ in range(4)])
    strategy = SlidingWindow(1) if mode == "sliding" else Summarize(summary_provider, 1, 1)
    children = []

    def factory(_profile):
        responses = [
            ProviderResponse([ToolCall("missing", "missing_tool", {})], "tool_use"),
            done(),
        ]
        provider = StreamingProvider(responses) if streaming else MockProvider(responses)
        child = agent(
            tmp_path,
            provider,
            name="worker",
            compaction=CompactionManager(summary_provider, strategy),
        )
        children.append(child)
        return child

    _, tool = delegator(tmp_path, factory)
    tasks = ["  Focused α\n", "New focused β\n"]
    origins = [" Exact origin Ω\n", "New origin λ\n"]
    root = agent(
        tmp_path,
        MockProvider([call(tasks[0]), done(), call(tasks[1]), done()]),
        [tool],
        name="root",
    )
    for origin in origins:
        await root.send(origin)
    calls = [entry for child in children for entry in child.provider.calls]
    assert len(calls) == 4
    assert len(children) == 2
    for index, (messages, _) in enumerate(calls):
        invocation = index // 2
        assert all(origin not in text_context(messages) for origin in origins)
        assignments = [
            block.text
            for message in messages
            for block in message.content
            if isinstance(block, TextBlock) and block.text.startswith("Focused assigned task:")
        ]
        assert assignments == ["Focused assigned task:\n" + tasks[invocation]]
        if invocation:
            assert origins[0] not in str(messages)
            assert tasks[0] not in str(messages)
    assert all(origin not in str(child.messages) for child in children for origin in origins)


async def test_child_failure_remains_tool_error_and_clears_lifecycle(tmp_path):
    child = agent(tmp_path, MockProvider(), name="worker")
    manager, tool = delegator(tmp_path, lambda _profile: child)
    provider = MockProvider([call(), done()])
    await agent(tmp_path, provider, [tool], name="root").send("Origin")
    result = provider.calls[1][0][-1].content[0]
    assert isinstance(result, ToolResult) and result.is_error
    assert manager.active() == []


def test_runtime_guidance_is_provider_neutral():
    prompt = root_system_prompt("Provider base")
    assert prompt.startswith("Provider base\n\n")
    assert ROOT_INSTRUCTIONS.strip() in prompt
    assert "Required approvals still apply" in prompt


async def test_unnamed_root_and_direct_manager_use_same_task_only_handoff(tmp_path):
    children = []

    def factory(_profile):
        child = agent(tmp_path, MockProvider([done()]))
        children.append(child)
        return child

    manager, tool = delegator(tmp_path, factory)
    root = agent(tmp_path, MockProvider([call(), done()]), [tool])
    await root.send("Unnamed root origin")
    assert children[0].provider.calls[0][0][0].content[0].text == "Focused assigned task:\nInspect"
    assert "Unnamed root origin" not in str(children[0].provider.calls)
    await manager.delegate("worker", "Direct task")
    assert "Unnamed root origin" not in str(children[1].provider.calls[0][0])
    assert children[1].provider.calls[0][0][-1].content[0].text.endswith("Direct task")


async def test_streaming_child_receives_only_focused_task(tmp_path):
    class StreamingMock(MockProvider):
        async def stream(self, messages, tools):
            yield ProviderResponseCompleted(await self.send(messages, tools))

    child = agent(tmp_path, StreamingMock([done()]))
    _, tool = delegator(tmp_path, lambda _profile: child)
    root = agent(tmp_path, MockProvider([call(), done()]), [tool])
    await root.send("Stream origin")
    assert len(child.provider.calls[0][0]) == 1
    assert child.provider.calls[0][0][0].content[0].text == "Focused assigned task:\nInspect"
    assert "Stream origin" not in str(child.provider.calls)
    assert "Stream origin" not in str(child.messages)
    assert "Stream origin" in text_context(root.messages)


async def test_cancelled_child_clears_active_record_and_incomplete_histories(tmp_path):
    async def cancel(_messages, _tools):
        assert _messages[0].content[0].text == "Focused assigned task:\nInspect"
        assert "Cancelled origin" not in str(_messages)
        raise asyncio.CancelledError

    child = agent(tmp_path, MockProvider(responder=cancel))
    manager, tool = delegator(tmp_path, lambda _profile: child)
    root = agent(tmp_path, MockProvider([call()]), [tool])
    with pytest.raises(asyncio.CancelledError):
        await root.send("Cancelled origin")
    assert manager.active() == []
    assert manager.records()[0].status.value == "cancelled"
    assert root.messages == child.messages == []


def test_handoff_retains_exact_task_and_separate_host_notices():
    task = "  Focused α\nexact input\n"
    assert render_handoff(task, []) == "Focused assigned task:\n" + task
    assert render_handoff(task, ["Unavailable artifact", "Access requires approval"]) == (
        "Focused assigned task:\n"
        + task
        + "\n\nHost notices:\nUnavailable artifact\nAccess requires approval"
    )


def test_root_runtime_assembles_guidance_and_project_context(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = parse_config(["--provider", "openai", "--working-dir", str(tmp_path)])
    project = ProjectContext(tmp_path, root_content="Private root project instructions")
    root_provider = build_provider(config, project)
    assert ROOT_INSTRUCTIONS.strip() in root_provider.system
    assert "Private root project instructions" in root_provider.system
    child_base = build_provider(config)
    assert ROOT_INSTRUCTIONS.strip() not in child_base.system
    assert "Private root project instructions" not in child_base.system
