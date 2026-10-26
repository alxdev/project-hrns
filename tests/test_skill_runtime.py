from pathlib import Path

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolResult
from harness.compaction import CompactionManager, SlidingWindow, Summarize
from harness.permissions import AlwaysAllow
from harness.provider import MockProvider
from harness.skills import load_skills
from harness.tools.base import ToolContext
from harness.tools.loader import load_tools
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


def make_catalog(tmp_path):
    root = tmp_path / ".agents" / "skills" / "example"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example workflow\n---\nFULL INSTRUCTIONS\n"
    )
    (tmp_path / "README.md").write_text("UNRELATED OUTPUT")
    return load_skills(tmp_path / "skills.json", tmp_path, tmp_path / "user")


async def test_successful_activation_is_durable_and_deduplicated_including_batches(
    tmp_path, monkeypatch
):
    catalog = make_catalog(tmp_path)
    original = catalog.activate
    attempts = []

    def activate(name, allowed):
        attempts.append(name)
        return original(name, allowed)

    monkeypatch.setattr(catalog, "activate", activate)
    provider = MockProvider(
        [
            ProviderResponse(
                [
                    ToolCall("first", "activate_skill", {"name": "example"}),
                    ToolCall("read", "read_file", {"path": "README.md"}),
                    ToolCall("again-batch", "activate_skill", {"name": "example"}),
                ],
                "tool_use",
            ),
            ProviderResponse(
                [
                    ToolCall("again", "activate_skill", {"name": "example"}),
                    ToolCall("failed", "activate_skill", {"name": "unassigned"}),
                ],
                "tool_use",
            ),
            ProviderResponse([TextBlock("Done")], "end_turn"),
        ]
    )
    agent = Agent(
        provider,
        ToolRegistry(load_tools(TOOLS)),
        AlwaysAllow(),
        ToolContext(tmp_path, skill_catalog=catalog, allowed_skills=("example",)),
    )

    await agent.send("Use the example skill")

    protected = [message for message in agent.messages if message.preserve_from_compaction]
    assert len(protected) == 1
    assert protected[0].skill_name == "example"
    assert protected[0].role == "user"
    assert "FULL INSTRUCTIONS" in protected[0].content[0].text
    assert "UNRELATED OUTPUT" not in protected[0].content[0].text
    assert attempts == ["example", "unassigned"]
    results = [
        block
        for message in agent.messages
        for block in message.content
        if isinstance(block, ToolResult)
    ]
    assert next(block for block in results if block.tool_use_id == "failed").is_error
    assert all("FULL INSTRUCTIONS" not in block.content for block in results)


@pytest.mark.parametrize("mode", ["sliding", "summarize"])
async def test_real_activation_after_file_read_survives_compaction(mode, tmp_path):
    catalog = make_catalog(tmp_path)
    provider = MockProvider(
        [
            ProviderResponse([ToolCall("read", "read_file", {"path": "README.md"})], "tool_use"),
            ProviderResponse(
                [ToolCall("skill", "activate_skill", {"name": "example"})], "tool_use"
            ),
            ProviderResponse([TextBlock("Done")], "end_turn"),
        ]
    )
    summaries = MockProvider(
        [ProviderResponse([TextBlock("Earlier task")], "end_turn") for _ in range(4)]
    )
    manager = CompactionManager(summaries)
    manager.strategy = (
        SlidingWindow(keep_last=1)
        if mode == "sliding"
        else Summarize(summaries, threshold=1, keep_recent=1)
    )
    agent = Agent(
        provider,
        ToolRegistry(load_tools(TOOLS)),
        AlwaysAllow(),
        ToolContext(tmp_path, skill_catalog=catalog, allowed_skills=("example",)),
        compaction=manager,
    )

    await agent.send("Use the skill")

    final_request = provider.calls[-1][0]
    assert len([message for message in final_request if message.skill_name == "example"]) == 1
    assert any(
        "FULL INSTRUCTIONS" in block.text
        for message in final_request
        for block in message.content
        if isinstance(block, TextBlock)
    )
    assert not any(
        isinstance(block, ToolResult) for message in final_request for block in message.content
    )


async def test_requesting_activation_tool_does_not_grant_unassigned_skills(tmp_path):
    catalog = make_catalog(tmp_path)
    tools = {tool.definition.name: tool for tool in load_tools(TOOLS)}
    provider = MockProvider(
        [
            ProviderResponse(
                [
                    ToolCall(
                        "request",
                        "request_tool",
                        {"name": "activate_skill", "reason": "Use a workflow"},
                    )
                ],
                "tool_use",
            ),
            ProviderResponse(
                [ToolCall("skill", "activate_skill", {"name": "example"})], "tool_use"
            ),
            ProviderResponse([TextBlock("Unavailable")], "end_turn"),
        ]
    )
    agent = Agent(
        provider,
        ToolRegistry([tools["request_tool"]]),
        AlwaysAllow(),
        ToolContext(tmp_path, skill_catalog=catalog, allowed_skills=(), tool_catalog=tools),
    )

    await agent.send("Try the skill")

    result = provider.calls[-1][0][-1].content[0]
    assert result.is_error
    assert "not allowed" in result.content
    assert not any(message.preserve_from_compaction for message in agent.messages)
