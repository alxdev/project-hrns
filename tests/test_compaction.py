from pathlib import Path

import pytest

from harness.api.types import Message, ProviderResponse, TextBlock, ToolCall, ToolResult
from harness.compaction import (
    CompactionManager,
    LoggingCompaction,
    SlidingWindow,
    Summarize,
    safe_split_point,
)
from harness.provider import MockProvider


def history():
    return [
        Message("user", [TextBlock("first")]),
        Message("assistant", [ToolCall("call-1", "read_file", {"path": "a.txt"})]),
        Message("user", [ToolResult("call-1", "contents")]),
        Message("assistant", [TextBlock("second")]),
        Message("user", [TextBlock("third")]),
    ]


async def test_safe_split_does_not_start_with_tool_result():
    messages = history()

    split = safe_split_point(messages, 2)

    assert split == 1
    assert not any(isinstance(block, ToolResult) for block in messages[split].content)


async def test_sliding_window_preserves_tool_pair():
    compacted = await SlidingWindow(keep_last=3).compact(history())

    assert len(compacted) == 4
    assert isinstance(compacted[0].content[0], ToolCall)
    assert isinstance(compacted[1].content[0], ToolResult)


async def test_sliding_window_preserves_skill_activation():
    messages = [
        Message("user", [TextBlock("activate the skill")]),
        Message(
            "user",
            [TextBlock("skill instructions")],
            preserve_from_compaction=True,
            kind="skill_activation",
            skill_name="example",
        ),
        Message("user", [TextBlock("continue")]),
        Message("assistant", [TextBlock("recent answer")]),
    ]

    compacted = await SlidingWindow(keep_last=1).compact(messages)

    assert any(message.kind == "skill_activation" for message in compacted)
    assert any(
        isinstance(message.content[0], TextBlock)
        and message.content[0].text == "skill instructions"
        for message in compacted
    )
    assert compacted[0].role == "user"


async def test_summarize_replaces_old_history():
    provider = MockProvider([ProviderResponse([TextBlock("summary")], "end_turn")])

    compacted = await Summarize(provider, threshold=1, keep_recent=2).compact(history())

    assert len(compacted) == 3
    assert isinstance(compacted[0].content[0], TextBlock)
    assert "summary" in compacted[0].content[0].text


async def test_logging_strategy_writes_only_when_history_changes(tmp_path: Path):
    log_path = tmp_path / "compactions.jsonl"
    strategy = LoggingCompaction(SlidingWindow(keep_last=2), log_path)

    await strategy.compact(history())

    assert log_path.exists()
    assert '"before_count": 5' in log_path.read_text(encoding="utf-8")


async def test_manager_report_describes_strategy_and_change():
    manager = CompactionManager(MockProvider(), strategy=SlidingWindow(keep_last=3))

    updated, report = await manager.run(history())

    assert len(updated) == 4
    assert report.strategy == "sliding"
    assert report.before_count == 5
    assert report.after_count == 4
    assert report.changed is True


def assert_valid_tool_history(messages):
    pending = set()
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolCall):
                assert block.id not in pending
                pending.add(block.id)
            elif isinstance(block, ToolResult):
                assert block.tool_use_id in pending
                pending.remove(block.tool_use_id)
        if pending:
            assert message.role == "assistant"
    assert not pending


@pytest.mark.parametrize("mode", ["sliding", "summarize"])
async def test_compaction_preserves_complete_legacy_activation_exchanges(mode):
    messages = [
        Message("user", [TextBlock("Inspect")]),
        Message("assistant", [ToolCall("read", "read_file", {"path": "README.md"})]),
        Message("user", [ToolResult("read", "unrelated file")]),
        Message(
            "assistant",
            [ToolCall("skill", "activate_skill", {"name": "example"})],
            preserve_from_compaction=True,
        ),
        Message(
            "user", [ToolResult("skill", "FULL SKILL INSTRUCTIONS")], preserve_from_compaction=True
        ),
        Message("assistant", [TextBlock("Done")]),
        Message("user", [TextBlock("Next")]),
    ]
    provider = MockProvider(
        [ProviderResponse([TextBlock("summary")], "end_turn") for _ in range(3)]
    )
    strategy = (
        SlidingWindow(keep_last=1)
        if mode == "sliding"
        else Summarize(provider, threshold=1, keep_recent=1)
    )

    for _ in range(3):
        messages = await strategy.compact(messages)
        assert_valid_tool_history(messages)
        assert any(
            isinstance(block, ToolResult) and block.content == "FULL SKILL INSTRUCTIONS"
            for message in messages
            for block in message.content
        )


@pytest.mark.parametrize("mode", ["sliding", "summarize"])
async def test_durable_skill_survives_without_retaining_unrelated_tool_output(mode):
    skill = Message(
        "user",
        [TextBlock("FULL INSTRUCTIONS\n" * 100)],
        preserve_from_compaction=True,
        kind="skill_activation",
        skill_name="example",
    )
    messages = [*history()[:3], skill, *history()[3:]]
    provider = MockProvider([ProviderResponse([TextBlock("summary")], "end_turn")])
    strategy = (
        SlidingWindow(keep_last=1)
        if mode == "sliding"
        else Summarize(provider, threshold=1, keep_recent=1)
    )

    result = await strategy.compact(messages)

    assert skill in result
    assert result.count(skill) == 1
    assert_valid_tool_history(result)
    assert not any(isinstance(block, ToolResult) for message in result for block in message.content)
    if mode == "summarize":
        assert "FULL INSTRUCTIONS" not in provider.calls[0][0][0].content[0].text
