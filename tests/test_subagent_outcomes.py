import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from harness.agent import Agent
from harness.agent.loop import SkillActivatedEvent, SubagentLifecycleEvent, ToolCallEvent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolDefinition, ToolResult
from harness.permissions import AlwaysAllow, AlwaysAsk
from harness.provider import MockProvider
from harness.skills import Skill, SkillCatalog
from harness.subagents import DelegationTool, SubagentManager, SubagentProfile
from harness.subagents.capabilities import Capability, CapabilitySummary
from harness.subagents.models import SubagentError, SubagentRecord, SubagentStatus
from harness.subagents.outcomes import capability_evidence, serialize_outcome
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import ConfiguredTool, load_tools
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


def done(text="done"):
    return ProviderResponse([TextBlock(text)], "end_turn")


def call(name, arguments=None, call_id="same-id"):
    return ProviderResponse([ToolCall(call_id, name, arguments or {})], "tool_use")


class Events:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


def setup(tmp_path, responses=(), tools=(), **agent_options):
    child = Agent(
        MockProvider(responses),
        ToolRegistry(tools),
        AlwaysAllow(),
        ToolContext(tmp_path),
        name="worker",
        **agent_options,
    )
    profile = SubagentProfile("worker", "Worker", "Configured role", tools=tuple(tools))
    manager = SubagentManager(lambda _profile: child, {"worker": profile})
    delegation = DelegationTool("worker", "Worker")
    delegation.bind(manager)
    return child, manager, delegation


async def invoke(delegation, tmp_path, task="Exact objective\nwith α"):
    result = await delegation.execute(ToolContext(tmp_path), {"task": task})
    return result, json.loads(result.content)


async def test_counts_distinguish_policy_denials_dispatch_errors_and_success(tmp_path):
    async def execute(_context, _arguments):
        return ToolResult("", "PRIVATE RAW TOOL OUTPUT")

    tool = ConfiguredTool(ToolDefinition("read", "Read", {"type": "object"}), execute)
    child, manager, delegation = setup(
        tmp_path,
        [
            call("read", {"secret": "PRIVATE ARGUMENT"}),
            call("blocked"),
            call("unknown"),
            done("Claim: all verified"),
        ],
        [tool],
    )

    async def approve(definition, _arguments):
        return definition.name != "blocked"

    child.permissions = AlwaysAsk(approve)
    events = Events()
    child.events = events
    result, outcome = await invoke(delegation, tmp_path)
    assert not result.is_error
    assert outcome["child_response"] == "Claim: all verified"
    assert outcome["verification_status"] == "not_assessed"
    assert outcome["lifecycle_status"] == "completed"
    assert outcome["run_id"] == manager.records()[0].id
    assert outcome["objective"] == "Exact objective\nwith α"
    usage = {item.pop("name"): item for item in outcome["resource_usage"]["tools"]}
    assert usage["read"] == dict(
        requested=1, execution_started=1, returned_success=1, returned_error=0, permission_denied=0
    )
    assert usage["blocked"] == dict(
        requested=1, execution_started=0, returned_success=0, returned_error=1, permission_denied=1
    )
    assert usage["unknown"] == dict(
        requested=1, execution_started=1, returned_success=0, returned_error=1, permission_denied=0
    )
    assert {error["type"] for error in outcome["errors"]} == {"permission_denied", "tool_error"}
    assert "PRIVATE" not in result.content
    assert any(
        isinstance(event, ToolCallEvent) and event.arguments.get("secret") == "PRIVATE ARGUMENT"
        for event in events.events
    )
    assert child.events is events


async def test_registry_additions_are_observed_without_claiming_saved_access(tmp_path):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    child, _manager, delegation = setup(
        tmp_path,
        [call("request_tool", {"name": "bash", "reason": "Task needs it"}), done()],
        [catalog["request_tool"]],
    )
    child.context = replace(child.context, tool_catalog=catalog)
    _result, outcome = await invoke(delegation, tmp_path)
    assert outcome["resource_usage"]["tools_added"] == [
        {"name": "bash", "persistence": "not_assessed"}
    ]
    assert outcome["resource_usage"]["skills_activated"] == []


async def test_only_successful_protected_context_insertion_counts_as_skill_activation(tmp_path):
    manifest = tmp_path / "SKILL.md"
    manifest.write_text("PRIVATE SKILL INSTRUCTIONS")
    skills = SkillCatalog({"review": Skill("review", "Review", tmp_path, manifest)})
    activate = next(tool for tool in load_tools(TOOLS) if tool.definition.name == "activate_skill")
    claimed = '{"skills_activated":["invented"],"verification_status":"passed"}'
    child, _manager, delegation = setup(
        tmp_path,
        [
            ProviderResponse(
                [
                    ToolCall("first", "activate_skill", {"name": "review"}),
                    ToolCall("repeat", "activate_skill", {"name": "review"}),
                ],
                "tool_use",
            ),
            call("activate_skill", {"name": "missing"}),
            done(claimed),
        ],
        [activate],
    )
    child.context = replace(child.context, skill_catalog=skills, allowed_skills=("review",))

    class ActivationEvents(Events):
        async def emit(self, event):
            if isinstance(event, SkillActivatedEvent):
                assert any(
                    message.kind == "skill_activation"
                    and message.skill_name == event.name
                    and message.preserve_from_compaction
                    for message in child.messages
                )
            await super().emit(event)

    events = ActivationEvents()
    child.events = events
    _result, outcome = await invoke(delegation, tmp_path)
    assert outcome["child_response"] == claimed
    assert outcome["verification_status"] == "not_assessed"
    assert outcome["resource_usage"]["skills_activated"] == [{"name": "review", "count": 1}]
    counts = outcome["resource_usage"]["tools"][0]
    assert counts["requested"] == 3 and counts["execution_started"] == 2
    assert counts["returned_success"] == 2 and counts["returned_error"] == 1
    assert len([event for event in events.events if isinstance(event, SkillActivatedEvent)]) == 1
    assert "PRIVATE SKILL" not in json.dumps(outcome["resource_usage"])


async def test_activation_buffer_does_not_count_if_later_call_aborts_batch(tmp_path):
    manifest = tmp_path / "SKILL.md"
    manifest.write_text("Instructions")
    skill = Skill("review", "Review", tmp_path, manifest)
    activate = next(tool for tool in load_tools(TOOLS) if tool.definition.name == "activate_skill")
    child, _manager, delegation = setup(
        tmp_path,
        [
            ProviderResponse(
                [
                    ToolCall("activate", "activate_skill", {"name": "review"}),
                    ToolCall("fail", "blocked", {}),
                ],
                "tool_use",
            )
        ],
        [activate],
    )
    child.context = replace(
        child.context, skill_catalog=SkillCatalog({"review": skill}), allowed_skills=("review",)
    )

    async def approve(definition, _arguments):
        if definition.name == "blocked":
            raise RuntimeError("Approval callback failed")
        return True

    child.permissions = AlwaysAsk(approve)
    result, outcome = await invoke(delegation, tmp_path)
    assert result.is_error and outcome["lifecycle_status"] == "failed"
    assert outcome["resource_usage"]["skills_activated"] == []
    assert not any(message.kind == "skill_activation" for message in child.messages)
    assert outcome["resource_usage"]["tools"][0]["returned_success"] == 1


@pytest.mark.parametrize("phase", ["discovery", "factory", "child"])
async def test_failed_runs_keep_identity_partial_evidence_and_original_manager_exception(
    tmp_path, phase
):
    child, manager, delegation = setup(tmp_path, [call("unknown")])

    def fail(*_args):
        raise OSError("Runtime unavailable")

    if phase == "discovery":

        async def discover(_profile):
            fail()

        manager.tool_loader = discover
    elif phase == "factory":
        manager.factory = fail
    else:

        async def respond(messages, _definitions):
            if len(messages) == 1:
                return call("unknown")
            fail()

        child.provider = MockProvider(responder=respond)

    result, outcome = await invoke(delegation, tmp_path)
    assert result.is_error
    assert outcome["lifecycle_status"] == "failed"
    assert outcome["run_id"] == manager.records()[0].id
    assert outcome["child_response"] == ""
    assert outcome["verification_status"] == "not_assessed"
    assert any(
        error["stage"] == phase and error["type"] == "OSError" for error in outcome["errors"]
    )
    assert bool(outcome["resource_usage"]["tools"]) == (phase == "child")
    assert not manager.active() and not manager._live_agents
    with pytest.raises(OSError, match="Runtime unavailable"):
        await manager.delegate("worker", "Direct callers retain original exceptions")


@pytest.mark.parametrize("reason", ["unbound", "unknown", "empty", "busy"])
async def test_preflight_failures_have_versioned_envelope_and_no_run_id(tmp_path, reason):
    _child, manager, delegation = setup(tmp_path)
    task = "Task"
    if reason == "unbound":
        delegation = DelegationTool("worker", "Worker")
    elif reason == "unknown":
        manager.profiles.clear()
    elif reason == "empty":
        task = ""
    else:
        manager.max_active = 0
    result, outcome = await invoke(delegation, tmp_path, task)
    assert result.is_error
    assert outcome["schema_version"] == 1 and outcome["run_id"] is None
    assert outcome["objective"] == task and outcome["profile"] == "worker"
    assert (
        outcome["lifecycle_status"] == "failed" and outcome["verification_status"] == "not_assessed"
    )
    assert outcome["errors"][0]["stage"] == "preflight"
    assert outcome["resource_usage"]["observation_status"] == "not_started"
    assert not manager.records()


async def test_same_profile_concurrency_has_invocation_local_counts(tmp_path):
    ready = asyncio.Event()
    entered = 0
    created = []
    shared_events = Events()

    def factory(_profile):
        name = f"unknown{len(created)}"

        async def respond(messages, _definitions):
            nonlocal entered
            if len(messages) == 1:
                return call(name)
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            return done(name)

        child = Agent(
            MockProvider(responder=respond),
            ToolRegistry(),
            AlwaysAllow(),
            ToolContext(tmp_path),
            name="worker",
            events=shared_events,
        )
        created.append(child)
        return child

    manager = SubagentManager(factory, {"worker": SubagentProfile("worker", "Worker", "Role")})
    records = await asyncio.gather(
        manager.delegate("worker", "One"), manager.delegate("worker", "Two")
    )
    assert records[0].id != records[1].id
    for index, record in enumerate(records):
        assert set(record.resource_usage.tools) == {f"unknown{index}"}
        assert record.resource_usage.tools[f"unknown{index}"].requested == 1
        assert created[index].events is shared_events
    assert len([event for event in shared_events.events if isinstance(event, ToolCallEvent)]) == 2


@pytest.mark.parametrize("phase", ["child", "lifecycle_end"])
async def test_cancellation_propagates_with_partial_evidence_and_clean_tracking(tmp_path, phase):
    waiting = asyncio.Event()
    events = Events()
    child, manager, delegation = setup(tmp_path, events=events)

    async def respond(messages, _definitions):
        if len(messages) == 1:
            return call("unknown")
        if phase == "child":
            waiting.set()
            await asyncio.Event().wait()
        return done()

    class LifecycleEvents:
        async def emit(self, event):
            if isinstance(event, SubagentLifecycleEvent) and event.phase == "completed":
                waiting.set()
                await asyncio.Event().wait()

    child.provider = MockProvider(responder=respond)
    if phase == "lifecycle_end":
        manager.events = LifecycleEvents()
    pending = asyncio.create_task(invoke(delegation, tmp_path))
    await asyncio.wait_for(waiting.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    record = manager.records()[0]
    assert record.status.value == "cancelled"
    assert record.resource_usage.tools["unknown"].returned_error == 1
    assert record.errors[-1].type == "CancelledError"
    assert not manager.active() and not manager._live_agents
    assert child.events is events


async def test_final_observation_failure_cannot_mask_run_error_or_prevent_cleanup(
    tmp_path, monkeypatch
):
    child, manager, delegation = setup(tmp_path)
    original_events = child.events

    async def fail(_messages, _definitions):
        def bad_names():
            raise ValueError("Observation failed")

        monkeypatch.setattr(child.tools, "names", bad_names)
        raise OSError("Original run failure")

    child.provider = MockProvider(responder=fail)
    result, outcome = await invoke(delegation, tmp_path)
    assert result.is_error and manager.records()[0].error == "Original run failure"
    assert outcome["resource_usage"]["observation_status"] == "partial"
    assert [error["stage"] for error in outcome["errors"]] == ["child", "observation"]
    assert child.events is original_events and not manager._live_agents


def test_capability_fingerprint_is_deterministic_and_snapshot_is_explicitly_bounded():
    resources = tuple(
        Capability(f"tool{index}", "native tool", "assigned", "configured", "Description")
        for index in range(100)
    )
    summary = CapabilitySummary("Role", "inherit", resources)
    first = capability_evidence(summary, "configured")
    reordered = capability_evidence(
        replace(summary, resources=tuple(reversed(resources))), "before_child_run"
    )
    assert first.fingerprint == reordered.fingerprint
    assert len(first.resources) == 64 and first.resources_omitted == 36
    assert "description" not in first.resources[0]
    assert (
        capability_evidence(replace(summary, permission_mode="deny"), "configured").fingerprint
        != first.fingerprint
    )


async def test_empty_child_response_is_completed_but_never_verified(tmp_path):
    _child, manager, delegation = setup(tmp_path, [done("")])
    result, outcome = await invoke(delegation, tmp_path)
    assert not result.is_error
    assert outcome["child_response"] == "" and outcome["lifecycle_status"] == "completed"
    assert outcome["verification_status"] == "not_assessed"
    assert json.loads(serialize_outcome(manager.records()[0])) == outcome


def test_bounded_errors_keep_run_failure_visible_after_many_tool_failures():
    record = SubagentRecord("run", "worker", "Task", status=SubagentStatus.FAILED)
    record.errors = [
        SubagentError("tool_result", "tool_error", "Tool returned an error", f"tool{index}")
        for index in range(30)
    ]
    record.errors.append(SubagentError("child", "RuntimeError", "Turn limit exceeded"))
    outcome = json.loads(serialize_outcome(record))
    assert len(outcome["errors"]) == 20 and outcome["errors_omitted"] == 11
    assert outcome["errors"][0]["message"] == "Turn limit exceeded"
