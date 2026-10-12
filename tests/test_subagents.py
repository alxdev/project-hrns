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
    assert len(created) == 2
    assert created[0].prompts == ["inspect the project"]
    assert created[1].prompts == ["inspect the tests"]
    assert manager.active() == []
    assert first.id != second.id


async def test_delegation_tool_returns_subagent_result():
    def factory(_profile):
        return FakeAgent()

    profiles = build_default_profiles([])
    manager = SubagentManager(factory, profiles)
    tool = DelegationTool("explorer", profiles["explorer"].description)
    tool.bind(manager)

    result = await tool.execute(
        ToolContext(Path.cwd()),
        {"task": "find the entry point"},
    )

    assert result.is_error is False
    assert "completed" in result.content


def test_delegation_tool_exposes_profile_schema():
    tool = DelegationTool("explorer", "Investigate the codebase.")

    assert tool.definition == ToolDefinition(
        name="delegate_explorer",
        description="Investigate the codebase.",
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The focused task to investigate or complete.",
                },
            },
            "required": ["task"],
        },
    )
