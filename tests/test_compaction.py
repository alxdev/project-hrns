from pathlib import Path

from harness.api.types import Message, ProviderResponse, TextBlock, ToolCall, ToolResult
from harness.compaction import CompactionManager, LoggingCompaction, SlidingWindow, Summarize, safe_split_point
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
