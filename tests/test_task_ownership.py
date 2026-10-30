"""Scripted ownership wiring, not evidence of autonomous model compliance."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolDefinition, ToolResult
from harness.orchestration import ROOT_INSTRUCTIONS, root_system_prompt
from harness.permissions import AlwaysAllow, AlwaysAsk
from harness.provider import MockProvider
from harness.subagents import DelegationTool, SubagentManager, SubagentProfile
from harness.subagents.instructions import TASK_INSTRUCTIONS, assemble_system_prompt
from harness.tools.base import ToolContext
from harness.tools.loader import ConfiguredTool, load_tools
from harness.tools.registry import ToolRegistry

ROOT = Path(__file__).parents[1]


def test_outcome_guidance_and_profile_metadata_keep_task_only_api():
    profiles = json.loads((ROOT / "subagents.json").read_text())["profiles"]
    for name, config in profiles.items():
        definition = DelegationTool(name, config["description"]).definition
        assert definition.name == f"delegate_{name}"
        assert definition.description == config["description"]
        assert definition.input_schema["required"] == ["task"]
        assert set(definition.input_schema["properties"]) == {"task"}
        description = definition.input_schema["properties"]["task"]["description"]
        assert "complete bounded outcome" in description
        assert "exact targets, inputs, constraints" in description
        assert "harness approvals" in description
        prompt = assemble_system_prompt(config["system_prompt"])
        assert prompt.startswith(config["system_prompt"] + "\n\n")
        assert TASK_INSTRUCTIONS.strip() in prompt
    prompt = root_system_prompt("Provider role")
    assert ROOT_INSTRUCTIONS.strip() in prompt
    assert "explicitly selected profile" in prompt
    assert "same delegation" in prompt
    assert "intermediate step" in prompt
    assert "Lifecycle completion" in TASK_INSTRUCTIONS
    assert "discovery-only assignment" in TASK_INSTRUCTIONS
    assert "named tool is explicitly required" in TASK_INSTRUCTIONS


@pytest.mark.parametrize("scenario", ["complete", "deny", "discovery_only", "missing"])
async def test_child_owns_real_discovery_access_approval_and_outcome(tmp_path, scenario):
    tools = {tool.definition.name: tool for tool in load_tools(ROOT / "tools.json")}
    controls = [tools["list_available_tools"], tools["request_tool"]]
    actions = []
    approvals = []
    task = (
        "Discover status only"
        if scenario == "discovery_only"
        else "Use exact tool calculate with values [2, 3]; report output. No writes."
    )
    root_provider = MockProvider(
        [
            ProviderResponse(
                [
                    ToolCall(
                        "delegate",
                        "delegate_worker",
                        {"task": task},
                    )
                ],
                "tool_use",
            ),
            ProviderResponse([TextBlock("Received outcome")], "end_turn"),
        ]
    )

    async def calculate(_context, arguments):
        assert len(root_provider.calls) == 1
        actions.append(arguments)
        return ToolResult("", f"total={sum(arguments['values'])}")

    target = ConfiguredTool(
        ToolDefinition("calculate", "Calculate a total in process", {"type": "object"}), calculate
    )
    catalog = {tool.definition.name: tool for tool in controls}
    if scenario != "missing":
        catalog["calculate"] = target

    async def approve(definition, _arguments):
        assert len(root_provider.calls) == 1
        approvals.append(definition.name)
        return not (scenario == "deny" and definition.name == "calculate")

    reports = {
        "complete": "Observed total=5. Assigned calculation complete.",
        "deny": "Calculation denied; no calculation executed. Assigned result unavailable.",
        "discovery_only": "calculate is installed but unassigned. Discovery complete.",
        "missing": "calculate is not installed. Required named capability missing; task blocked.",
    }
    responses = [ProviderResponse([ToolCall("discover", "list_available_tools", {})], "tool_use")]
    if scenario != "discovery_only":
        responses.append(
            ProviderResponse(
                [ToolCall("request", "request_tool", {"name": "calculate", "reason": "Assigned"})],
                "tool_use",
            )
        )
    if scenario in {"complete", "deny"}:
        responses.append(
            ProviderResponse([ToolCall("action", "calculate", {"values": [2, 3]})], "tool_use")
        )
    responses.append(ProviderResponse([TextBlock(reports[scenario])], "end_turn"))

    async def respond(_messages, _definitions):
        # Root does not re-enter during any intermediate child model/tool step.
        assert len(root_provider.calls) == 1
        return responses.pop(0)

    child_provider = MockProvider(responder=respond)
    persist = AsyncMock(return_value=False)
    child = Agent(
        child_provider,
        ToolRegistry(controls),
        AlwaysAsk(approve),
        ToolContext(tmp_path, tool_catalog=catalog, persist_tool_access=persist),
        name="worker",
        system_prompt=assemble_system_prompt("Calculate or discover only as assigned."),
    )
    manager = SubagentManager(
        lambda _profile: child, {"worker": SubagentProfile("worker", "Worker", "Worker")}
    )
    delegate = DelegationTool("worker", "Worker")
    delegate.bind(manager)
    root = Agent(
        root_provider,
        ToolRegistry([delegate]),
        AlwaysAllow(),
        ToolContext(tmp_path),
        name="root",
        capture_request=True,
    )
    assert await root.send("Complete assigned workflow") == "Received outcome"
    assert len(root_provider.calls) == 2
    assigned = root_provider.calls[0][1][0]
    assert assigned.name == "delegate_worker"
    task = manager.records()[0].prompt
    assert child_provider.calls[0][0][-1].content[0].text.endswith(task)
    if scenario != "discovery_only":
        assert "exact tool calculate with values [2, 3]" in task and "No writes." in task
    result = root_provider.calls[1][0][-1].content[0]
    assert isinstance(result, ToolResult) and result.content.endswith(reports[scenario])
    assert not result.is_error  # Lifecycle return is not a task-success verdict.
    assert not responses
    assert actions == ([{"values": [2, 3]}] if scenario == "complete" else [])
    if scenario in {"complete", "deny"}:
        assert approvals == ["list_available_tools", "request_tool", "calculate"]
        persist.assert_awaited_once_with("calculate")
        access = child_provider.calls[2][0][-1].content[0]
        assert json.loads(access.content)["status"] == "enabled"
        observed = child_provider.calls[3][0][-1].content[0]
        assert observed.is_error == (scenario == "deny")
    elif scenario == "discovery_only":
        assert approvals == ["list_available_tools"]
        persist.assert_not_awaited()
        assert child.tools.get("calculate") is None
    else:
        assert approvals == ["list_available_tools", "request_tool"]
        persist.assert_not_awaited()
        missing = child_provider.calls[2][0][-1].content[0]
        assert missing.is_error and json.loads(missing.content)["status"] == "not_installed"


async def test_missing_capability_guidance_requires_explicit_user_creation_request(tmp_path):
    tools = {tool.definition.name: tool for tool in load_tools(ROOT / "tools.json")}
    registry = ToolRegistry([tools["request_tool"]])
    result = await registry.execute(
        ToolContext(tmp_path, tool_registry=registry, tool_catalog=tools),
        "request_tool",
        {"name": "missing", "reason": "Assigned operation needs it"},
    )
    payload = json.loads(result.content)
    assert result.is_error and payload["status"] == "not_installed"
    assert "Only if the user explicitly requested reusable tool creation" in payload["next_step"]
    assert "within your assigned scope" in payload["next_step"]
    assert "request_tool for tool_authoring" in payload["next_step"]
    assert "Otherwise report" in payload["next_step"]
    assert registry.get("tool_authoring") is None
