from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall
from harness.permissions import PermissionDecision
from harness.provider import MockProvider
from harness.tools.base import ToolContext
from harness.tools.read_file import ReadFileTool
from harness.tools.registry import ToolRegistry


class AllowAll:
    async def decide(self, tool, arguments):
        return PermissionDecision.ALLOW


async def test_agent_completes_after_tool_call(tmp_path):
    provider = MockProvider(
        responses=[
            ProviderResponse([ToolCall("call-1", "read_file", {"path": "hello.txt"})], "tool_use"),
            ProviderResponse([TextBlock("The file says hello.")], "end_turn"),
        ]
    )
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    agent = Agent(
        provider=provider,
        tools=ToolRegistry([ReadFileTool()]),
        permissions=AllowAll(),
        context=ToolContext(tmp_path),
    )

    answer = await agent.send("Read hello.txt")

    assert answer == "The file says hello."
    assert len(provider.calls) == 2
    assert provider.calls[1][0][-1].content[0].tool_use_id == "call-1"


async def test_denied_tool_is_returned_as_error_result(tmp_path):
    provider = MockProvider(
        responses=[
            ProviderResponse([ToolCall("call-1", "read_file", {"path": "hello.txt"})], "tool_use"),
            ProviderResponse([TextBlock("Understood.")], "end_turn"),
        ]
    )

    class DenyAll:
        async def decide(self, tool, arguments):
            return PermissionDecision.DENY

    agent = Agent(
        provider=provider,
        tools=ToolRegistry([ReadFileTool()]),
        permissions=DenyAll(),
        context=ToolContext(tmp_path),
    )

    await agent.send("Read hello.txt")

    denied = provider.calls[1][0][-1].content[0]
    assert denied.is_error is True
    assert denied.content == "user denied this tool call"

