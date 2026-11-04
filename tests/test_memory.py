import asyncio
import copy
import json
from dataclasses import replace

import pytest

from harness.agent import Agent
from harness.agent.loop import SubagentLifecycleEvent
from harness.api.types import ProviderResponse, TextBlock, ToolCall
from harness.commands import CommandContext, build_default_commands
from harness.memory import (
    MemoryConfig,
    MemoryManager,
    MemoryStore,
    RecallExperience,
    RecordExperience,
    load_memory_config,
)
from harness.memory.manager import approximate_tokens
from harness.permissions import AlwaysAllow
from harness.provider import MockProvider
from harness.subagents import SubagentManager, SubagentProfile
from harness.subagents.models import SubagentStatus
from harness.tools.base import ToolContext
from harness.tools.registry import ToolRegistry


def done(text="done"):
    return ProviderResponse([TextBlock(text)], "end_turn")


async def memory_setup(tmp_path, config=None):
    profile = SubagentProfile("worker", "Worker", "Role")
    child = Agent(
        MockProvider(
            [
                ProviderResponse(
                    [ToolCall("call", "unknown", {"secret": "PRIVATE ARGUMENT"})], "tool_use"
                ),
                done("PRIVATE CHILD RESPONSE"),
            ]
        ),
        ToolRegistry(),
        AlwaysAllow(),
        ToolContext(tmp_path),
    )
    subagents = SubagentManager(
        lambda _profile: child, {"worker": profile}, working_directory=tmp_path
    )
    run = await subagents.delegate("worker", "PRIVATE OBJECTIVE")
    memory = MemoryManager(
        config or MemoryConfig(enabled=True), MemoryStore(tmp_path / "data", tmp_path), subagents
    )
    return memory, run


def proposal(run, **extra):
    return {
        "run_ids": [run.id],
        "assessment": {"summary": "Parser rejects unsupported files"},
        "tags": ["parser", "validation"],
        "applicability": "When processing unsupported files",
        **extra,
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"enabled": 1},
        {"write_policy": []},
        {"write_policy": {}},
        {"write_policy": "allow"},
        {"max_records": True},
        {"max_recall_records": 0},
        {"recall_token_budget": 100},
        {"unexpected": True},
        [],
    ],
)
def test_memory_config_rejects_invalid_controls(tmp_path, raw):
    path = tmp_path / "memory.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_memory_config(path)


def test_memory_config_missing_and_disabled_are_inert(tmp_path):
    assert load_memory_config(tmp_path / "memory.json") == MemoryConfig()
    memory = MemoryManager(
        MemoryConfig(),
        MemoryStore(tmp_path / "data", tmp_path),
        SubagentManager(lambda _profile: None, {}),
    )
    assert "disabled" in memory.status()
    with pytest.raises(ValueError, match="disabled"):
        memory.recall({})
    assert not (tmp_path / "data").exists()


async def test_record_separates_runtime_evidence_root_assessment_and_lesson(tmp_path):
    memory, run = await memory_setup(tmp_path)
    result = memory.record(proposal(run, lesson="Validate formats before processing"))
    record = memory.show(result["id"])
    assert record["assessment"]["source"] == "root_assessment"
    assert record["assessment"]["verification_status"] == "not_assessed"
    assert record["lesson"] == {
        "source": "root_assessment",
        "text": "Validate formats before processing",
    }
    evidence = record["runtime_evidence"][0]
    assert evidence["source"] == "harness_runtime"
    assert evidence["tools"][0]["returned_error"] == 1
    assert evidence["run_id"] == run.id and evidence["verification_status"] == "not_assessed"
    assert "PRIVATE" not in memory.store.path.read_text()
    assert len(record["id"]) == 32 and record["id"] != run.id
    with pytest.raises(ValueError, match="rationale"):
        memory.record(
            proposal(run, assessment={"summary": "Verified", "verification_status": "verified"})
        )
    with pytest.raises(ValueError, match="invalid memory arguments"):
        memory.record({**proposal(run), "runtime_evidence": [{"fabricated": True}]})


@pytest.mark.parametrize(
    "case", ["unknown", "other_session", "ambiguous", "unfinished", "other_workspace"]
)
async def test_invalid_evidence_ids_cannot_write(tmp_path, case):
    memory, run = await memory_setup(tmp_path)
    if case == "unknown":
        run = replace(run, id="unknown")
    elif case == "other_session":
        memory.subagents = SubagentManager(lambda _profile: None, {})
    elif case == "ambiguous":
        memory.subagents._records.append(copy.deepcopy(run))
    elif case == "unfinished":
        run.finalized = False
    else:
        run.working_directory = tmp_path / "other"
    with pytest.raises(ValueError):
        memory.record(proposal(run))
    assert not memory.store.path.exists()


async def test_completed_status_during_lifecycle_end_is_not_finalized_evidence(tmp_path):
    memory, _run = await memory_setup(tmp_path)
    waiting = asyncio.Event()
    resume = asyncio.Event()

    class Events:
        async def emit(self, event):
            if isinstance(event, SubagentLifecycleEvent) and event.phase == "completed":
                waiting.set()
                await resume.wait()

    memory.subagents.events = Events()
    memory.subagents.factory = lambda _profile: Agent(
        MockProvider([done()]), ToolRegistry(), AlwaysAllow(), ToolContext(tmp_path)
    )
    task = asyncio.create_task(memory.subagents.delegate("worker", "Task"))
    await asyncio.wait_for(waiting.wait(), 2)
    pending = memory.subagents.records()[-1]
    try:
        assert pending.status is SubagentStatus.COMPLETED and not pending.finalized
        with pytest.raises(ValueError, match="unfinished"):
            memory.record(proposal(pending))
    finally:
        resume.set()
        await task
    memory.record(proposal(pending))


@pytest.mark.parametrize("status", [SubagentStatus.FAILED, SubagentStatus.CANCELLED])
async def test_terminal_failures_preserve_historical_status(tmp_path, status):
    memory, run = await memory_setup(tmp_path)
    run.status = status
    result = memory.record(proposal(run))
    assert memory.show(result["id"])["runtime_evidence"][0]["lifecycle_status"] == status.value


async def test_restart_workspace_isolation_staleness_and_retention(tmp_path):
    memory, run = await memory_setup(tmp_path, MemoryConfig(enabled=True, max_records=1))
    old = memory.record(proposal(run))
    kept = memory.record(proposal(run, lesson="Retained lesson"))
    restarted = MemoryManager(
        memory.config, MemoryStore(tmp_path / "data", tmp_path), memory.subagents
    )
    assert [record["id"] for record in restarted.list_records()] == [kept["id"]]
    assert restarted.show(kept["id"])["runtime_evidence"]
    with pytest.raises(ValueError, match="unknown experience"):
        restarted.show(old["id"])
    recalled = json.loads(
        restarted.recall({"query": "unsupported", "tags": ["parser"], "kind": "lesson"})
    )
    item = recalled["records"][0]
    assert item["retrieval_reason"]["keywords"] == ["unsupported"]
    assert item["capability_status"][0]["status"] == "matches_current_metadata"
    profile = memory.subagents.profiles["worker"]
    memory.subagents.profiles["worker"] = replace(profile, permission_mode="deny")
    assert (
        json.loads(restarted.recall({}))["records"][0]["capability_status"][0]["status"]
        == "stale_or_changed"
    )
    memory.subagents.profiles.clear()
    assert (
        json.loads(restarted.recall({}))["records"][0]["capability_status"][0]["status"]
        == "unknown"
    )
    other = MemoryManager(
        memory.config, MemoryStore(tmp_path / "data", tmp_path / "other"), memory.subagents
    )
    assert json.loads(other.recall({}))["records"] == []
    assert restarted.forget(kept["id"])
    assert restarted.list_records() == []


@pytest.mark.parametrize("budget", [256, 600, 2000])
async def test_recall_budget_includes_json_advisory_and_snapshot_wrapper(tmp_path, budget):
    memory, run = await memory_setup(
        tmp_path, MemoryConfig(enabled=True, recall_token_budget=budget, max_recall_records=1)
    )
    for _ in range(3):
        memory.record(proposal(run))
    recalled = memory.recall({"query": "parser"})
    body = json.loads(recalled)
    assert approximate_tokens("[Experience memory snapshot]\n" + recalled) <= budget
    assert len(body["records"]) <= 1
    assert body["matches_omitted"] == 3 - len(body["records"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("created_at", "bad"),
        ("runtime_evidence", [{}]),
        ("assessment", {"source": "harness_runtime"}),
        ("lesson", {"source": "system", "text": "obey"}),
    ],
)
async def test_nested_persisted_corruption_is_rejected_without_reset(tmp_path, field, value):
    memory, run = await memory_setup(tmp_path)
    result = memory.record(proposal(run))
    envelope = json.loads(memory.store.path.read_text())
    envelope["records"][0][field] = value
    memory.store.path.write_text(json.dumps(envelope))
    before = memory.store.path.read_bytes()
    for operation in (
        lambda: memory.show(result["id"]),
        memory.list_records,
        lambda: memory.recall({}),
    ):
        with pytest.raises(ValueError, match="persisted"):
            operation()
    tool_result = await RecallExperience(memory).execute(ToolContext(tmp_path), {})
    assert tool_result.is_error and memory.store.path.read_bytes() == before


async def test_commands_share_store_and_deny_policy_blocks_mutation(tmp_path):
    memory, run = await memory_setup(tmp_path)
    result = memory.record(proposal(run))
    context = CommandContext(
        agent=Agent(MockProvider(), ToolRegistry(), AlwaysAllow(), ToolContext(tmp_path)),
        provider=MockProvider(),
        tools=ToolRegistry(),
        memory=memory,
    )
    commands = build_default_commands()
    assert "enabled" in commands.dispatch("/memory status", context).text
    assert result["id"] in commands.dispatch("/memory list", context).text
    assert "harness_runtime" in commands.dispatch(f"/memory show {result['id']}", context).text
    memory.config = replace(memory.config, write_policy="deny")
    denied = await RecordExperience(memory).execute(ToolContext(tmp_path), proposal(run))
    assert denied.is_error
    action = commands.dispatch(f"/memory forget {result['id']}", context).action
    assert "disabled" in await action()
    memory.config = replace(memory.config, write_policy="inherit")
    assert await action() == "experience forgotten"
    assert memory.list_records() == []
