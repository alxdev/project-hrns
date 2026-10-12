from pathlib import Path

from harness.api.types import ToolDefinition
from harness.subagents import (
    DelegationTool,
    SubagentManager,
    SubagentStatus,
    build_default_profiles,
)
from harness.tools.base import ToolContext
from harness.tools.read_file import ReadFileTool
from harness.tools.write_file import WriteFileTool


class FakeAgent:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def send(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"handled: {prompt}"


def test_profiles_have_curated_tools():
    profiles = build_default_profiles([ReadFileTool(), WriteFileTool()])

    assert [tool.definition.name for tool in profiles["explorer"].tools] == ["read_file"]
    assert {tool.definition.name for tool in profiles["coder"].tools} == {
        "read_file",
        "write_file",
    }


async def test_manager_reuses_agent_and_tracks_records():
    created: list[FakeAgent] = []

    def factory(_profile):
        agent = FakeAgent()
        created.append(agent)
        return agent

    manager = SubagentManager(factory, build_default_profiles([]))

    first = await manager.delegate("explorer", "inspect the project")
    second = await manager.delegate("explorer", "inspect the tests")

    assert first.status is SubagentStatus.COMPLETED
    assert second.status is SubagentStatus.COMPLETED
    assert len(created) == 1
    assert created[0].prompts == ["inspect the project", "inspect the tests"]
    assert manager.active() == []
    assert first.id != second.id


async def test_delegation_tool_returns_subagent_result():
    def factory(_profile):
        return FakeAgent()

    profiles = build_default_profiles([])
    manager = SubagentManager(factory, profiles)
    tool = DelegationTool(sorted(profiles))
    tool.bind(manager)

    result = await tool.execute(
        ToolContext(Path.cwd()),
        {"profile": "explorer", "prompt": "find the entry point"},
    )

    assert result.is_error is False
    assert "completed" in result.content


def test_delegation_tool_exposes_profile_schema():
    tool = DelegationTool(["explorer", "coder"])

    assert tool.definition == ToolDefinition(
        name="delegate_to_agent",
        description="Delegate a focused task to a reusable subagent profile.",
        input_schema={
            "type": "object",
            "properties": {
                "profile": {"type": "string", "enum": ["explorer", "coder"]},
                "prompt": {"type": "string"},
            },
            "required": ["profile", "prompt"],
        },
    )
