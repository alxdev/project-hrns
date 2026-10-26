import json
import sys
from pathlib import Path

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolDefinition, ToolResult
from harness.permissions import PermissionDecision
from harness.provider import MockProvider
from harness.tools.base import ToolContext
from harness.tools.loader import load_tools
from harness.tools.registry import ToolRegistry

TOOLS_PATH = Path(__file__).parents[1] / "tools.json"


class AllowAll:
    async def decide(self, _tool, _arguments):
        return PermissionDecision.ALLOW


class FakeBash:
    @property
    def definition(self):
        return ToolDefinition("bash", "Run a command.", {"type": "object"})

    async def execute(self, _context, arguments):
        return ToolResult("", f"ran {arguments['command']}")


async def test_request_tool_adds_installed_tool_before_next_provider_call(tmp_path):
    provider = MockProvider(
        responses=[
            ProviderResponse(
                [ToolCall("request-1", "request_tool", {"name": "bash", "reason": "run tests"})],
                "tool_use",
            ),
            ProviderResponse([ToolCall("bash-1", "bash", {"command": "pytest"})], "tool_use"),
            ProviderResponse([TextBlock("Tests completed.")], "end_turn"),
        ]
    )
    request_tool = next(
        tool for tool in load_tools(TOOLS_PATH) if tool.definition.name == "request_tool"
    )
    agent = Agent(
        provider=provider,
        tools=ToolRegistry([request_tool]),
        permissions=AllowAll(),
        context=ToolContext(tmp_path, tool_catalog={"bash": FakeBash()}),
    )

    assert await agent.send("Run the tests") == "Tests completed."
    assert any(definition.name == "bash" for definition in provider.calls[1][1])
    assert provider.calls[2][0][-1].content[0].content == "ran pytest"


async def test_tool_authoring_creates_and_activates_user_artifact(tmp_path):
    tool_authoring = next(
        tool for tool in load_tools(TOOLS_PATH) if tool.definition.name == "tool_authoring"
    )
    registry = ToolRegistry()
    context = ToolContext(tmp_path, artifact_root=tmp_path / "artifacts", tool_registry=registry)
    result = await tool_authoring.execute(
        context,
        {
            "name": "math_helper",
            "description": "Add two numbers.",
            "input_schema": {"type": "object"},
            "files": {
                "run.py": (
                    'import json,sys\nargs=json.load(sys.stdin)\nprint(args["a"] + args["b"])\n'
                )
            },
            "command": [sys.executable, "run.py"],
        },
    )

    assert result.is_error is False
    assert (tmp_path / "artifacts" / "math_helper" / "manifest.json").exists()
    assert len(registry.definitions()) == 1
    output = await registry.execute(context, "math_helper", {"a": 2, "b": 3})
    assert output.content.strip() == "5"


async def test_bash_keeps_stderr_as_diagnostics_without_extra_model_output(tmp_path):
    bash = next(tool for tool in load_tools(TOOLS_PATH) if tool.definition.name == "bash")
    logs: list[tuple[str, str]] = []

    async def emit_log(name: str, text: str, stream: str) -> None:
        assert name == "bash"
        logs.append((text, stream))

    result = await bash.execute(
        ToolContext(tmp_path, emit_log=emit_log), {"command": "printf out; printf err >&2"}
    )

    assert result.is_error is False
    assert result.content == "out"
    assert "err" in "".join(text for text, _stream in logs)
    assert all(stream == "stderr" for _text, stream in logs)


async def test_unregistered_tool_request_cannot_grant_access(tmp_path):
    registry = ToolRegistry()
    provider = MockProvider(
        [
            ProviderResponse(
                [ToolCall("request", "request_tool", {"name": "bash", "reason": "Need it"})],
                "tool_use",
            ),
            ProviderResponse([TextBlock("Unavailable")], "end_turn"),
        ]
    )
    agent = Agent(
        provider, registry, AllowAll(), ToolContext(tmp_path, tool_catalog={"bash": FakeBash()})
    )

    await agent.send("Try requesting a tool that is disabled")

    result = provider.calls[-1][0][-1].content[0]
    assert result.is_error
    assert "unknown tool: request_tool" in result.content
    assert registry.definitions() == []


async def test_request_handler_works_under_a_configured_alias(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(
        json.dumps(
            {
                "tools": {
                    "ask_for_tool": {
                        "handler": "request_tool",
                        "description": "Request access",
                        "input_schema": {"type": "object"},
                    }
                }
            }
        )
    )
    registry = ToolRegistry(load_tools(path))
    provider = MockProvider(
        [
            ProviderResponse(
                [ToolCall("request", "ask_for_tool", {"name": "bash", "reason": "Need it"})],
                "tool_use",
            ),
            ProviderResponse([ToolCall("run", "bash", {"command": "example"})], "tool_use"),
            ProviderResponse([TextBlock("Done")], "end_turn"),
        ]
    )
    agent = Agent(
        provider, registry, AllowAll(), ToolContext(tmp_path, tool_catalog={"bash": FakeBash()})
    )

    await agent.send("Use the configured request handler")

    assert provider.calls[-1][0][-1].content[0].content == "ran example"
