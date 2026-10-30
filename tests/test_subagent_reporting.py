"""Instruction delivery and scripted handoffs, not guarantees of model compliance."""

import json
from pathlib import Path

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolDefinition, ToolResult
from harness.permissions import AlwaysAllow
from harness.provider import MockProvider
from harness.subagents import DelegationTool, SubagentManager, SubagentProfile
from harness.subagents.instructions import TASK_INSTRUCTIONS, assemble_system_prompt
from harness.tools.base import ToolContext
from harness.tools.loader import ConfiguredTool
from harness.tools.registry import ToolRegistry

TOOLS_PATH = Path(__file__).parents[1] / "tools.json"


def done(text):
    return ProviderResponse([TextBlock(text)], "end_turn")


@pytest.mark.parametrize("assigned", [[], ["read_file"], ["tool_authoring"]])
async def test_main_delivers_task_guidance_to_every_profile(tmp_path, monkeypatch, assigned):
    import harness.main as main_module

    (tmp_path / "tools.json").write_text(TOOLS_PATH.read_text())
    (tmp_path / "subagents.json").write_text(
        json.dumps(
            {
                "profiles": {
                    "worker": {
                        "description": "Focused worker",
                        "system_prompt": "Remain within the assigned read-only role.",
                        "permission_mode": "inherit",
                        "tools": assigned,
                        "skills": [],
                        "requires_tools": False,
                    }
                }
            }
        )
    )
    root = MockProvider(
        [
            ProviderResponse(
                [ToolCall("delegate", "delegate_worker", {"task": "Report a finding"})], "tool_use"
            ),
            done("Received finding"),
        ]
    )
    child = MockProvider([done("Actual finding")])
    root.system = "Root role"
    child.system = "Provider default"
    providers = iter([root, child])
    monkeypatch.setattr(main_module, "build_provider", lambda *_args: next(providers))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    assert (
        await main_module.main(
            ["--working-dir", str(tmp_path), "--prompt", "Report a finding", "--allow-tools"]
        )
        == 0
    )
    assert child.system.startswith("Remain within the assigned read-only role.")
    assert TASK_INSTRUCTIONS.strip() in child.system
    assert root.system == "Root role"
    definitions = {tool.name: tool for tool in child.calls[0][1]}
    assert ("tool_authoring" in definitions) == ("tool_authoring" in assigned)
    if "tool_authoring" in definitions:
        description = definitions["tool_authoring"].input_schema["properties"]["command"][
            "description"
        ]
        for phrase in (
            "runs as a subprocess",
            "serializes arguments as JSON on stdin",
            "not necessarily JSON",
            "does not invoke handlers automatically",
            "diagnostics to stderr",
            "without executing or proving correctness",
            "execution requires separate approval",
            "Empty stdout is not evidence",
        ):
            assert phrase in description
    else:
        assert "does not invoke handlers automatically" not in child.system


@pytest.mark.parametrize(
    ("output", "is_error", "report"),
    [
        ("words=3", False, "Observed output: words=3. Assigned analysis complete."),
        ("", False, "Expected output was absent despite successful exit; analysis incomplete."),
        ("execution denied", True, "Execution denied; analysis incomplete. No metrics observed."),
    ],
)
async def test_delegated_loop_returns_final_output_or_failure_facts(
    tmp_path, output, is_error, report
):
    calls = []

    async def acquire(_context, _arguments):
        calls.append("acquire")
        return ToolResult("", "Capability available; execution still required")

    async def execute(_context, _arguments):
        calls.append("execute")
        return ToolResult("", output, is_error=is_error)

    # These are mocked tool results; no artifact is created or executed.
    tools = [
        ConfiguredTool(ToolDefinition("acquire", "Acquire", {"type": "object"}), acquire),
        ConfiguredTool(ToolDefinition("execute", "Execute", {"type": "object"}), execute),
    ]
    provider = MockProvider(
        [
            ProviderResponse([ToolCall("acquire-call", "acquire", {})], "tool_use"),
            ProviderResponse([ToolCall("execute-call", "execute", {})], "tool_use"),
            done(report),
        ]
    )
    prompt = assemble_system_prompt("Perform only the assigned analysis.")
    provider.system = prompt
    child = Agent(
        provider,
        ToolRegistry(tools),
        AlwaysAllow(),
        ToolContext(tmp_path),
        name="worker",
        system_prompt=prompt,
    )
    manager = SubagentManager(
        lambda _profile: child, {"worker": SubagentProfile("worker", "Worker", prompt)}
    )
    delegate = DelegationTool("worker", "Worker")
    delegate.bind(manager)
    root_provider = MockProvider(
        [
            ProviderResponse(
                [ToolCall("delegate", "delegate_worker", {"task": "Acquire, execute, report"})],
                "tool_use",
            ),
            done("Received child report"),
        ]
    )
    root = Agent(
        root_provider,
        ToolRegistry([delegate]),
        AlwaysAllow(),
        ToolContext(tmp_path),
        name="root",
    )
    assert await root.send("Analyze and report") == "Received child report"

    assert calls == ["acquire", "execute"]
    assert len(provider.calls) == 3
    observed = provider.calls[2][0][-1].content[0]
    assert isinstance(observed, ToolResult)
    assert observed.content == output and observed.is_error == is_error
    assert manager.records()[0].result == report
    result = root_provider.calls[1][0][-1].content[0]
    assert isinstance(result, ToolResult)
    assert result.content.endswith(report)
    # A model reporting a blocker is still an ordinary final response to the manager.
    assert not result.is_error
