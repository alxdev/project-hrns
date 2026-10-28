import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall
from harness.permissions import AlwaysAllow, AlwaysDeny
from harness.provider import MockProvider
from harness.subagents import DelegationTool, SubagentManager, load_profiles
from harness.tools.access import make_profile_tool_access_persister
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import load_tools
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


async def create(catalog, context, arguments):
    context = replace(
        context,
        approve_artifact_preparation=AsyncMock(return_value=True),
        approve_artifact_creation=AsyncMock(return_value=True),
    )
    tool = catalog["tool_authoring"]
    assert not (await tool.execute(context, {"name": arguments["name"]})).is_error
    return await tool.execute(context, arguments)


def setup_profiles(tmp_path, catalog, reuse=False):
    path = tmp_path / "subagents.json"
    profile = {
        "description": "Lab",
        "system_prompt": "Test tools",
        "tools": [],
        "permission_mode": "inherit",
        "reuse": reuse,
    }
    path.write_text(json.dumps({"profiles": {"lab": profile, "other": profile}}))
    return path, load_profiles(path, catalog)


@pytest.mark.parametrize("kind", ["native", "artifact_request", "artifact_authoring"])
@pytest.mark.parametrize("outcome", ["saved", "denied", "failed", "existing"])
async def test_saved_access_reaches_next_fresh_delegation(tmp_path, monkeypatch, kind, outcome):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    path, profiles = setup_profiles(tmp_path, catalog)
    name = "bash" if kind == "native" else "runtime_helper"
    artifact_args = {
        "name": name,
        "description": "Print a marker",
        "input_schema": {"type": "object"},
        "files": {"run.py": 'print("executed")\n'},
        "command": [sys.executable, "run.py"],
    }
    if kind == "artifact_request":
        result = await create(
            catalog,
            ToolContext(tmp_path, artifact_root=catalog.artifact_root, tool_catalog=catalog),
            artifact_args,
        )
        assert not result.is_error
    if kind == "artifact_authoring":
        # The artifact is created after both manager and shared catalog exist.
        profiles["lab"] = replace(profiles["lab"], tools=(catalog["tool_authoring"],))
    if outcome == "existing":
        raw = json.loads(path.read_text())
        field = "tools" if kind == "native" else "tool_artifacts"
        raw["profiles"]["lab"].setdefault(field, []).append(name)
        path.write_text(json.dumps(raw))
    if outcome == "failed":

        def fail_write(*_args):
            raise OSError("simulated save failure")

        monkeypatch.setattr("harness.tools.access._atomic_write", fail_write)

    approvals = []
    errors = []
    created = []

    async def approve(_definition, arguments):
        approvals.append(arguments)
        # An unrelated external edit must survive on disk without being loaded.
        raw = json.loads(path.read_text())
        raw["profiles"]["lab"]["system_prompt"] = "External edit"
        path.write_text(json.dumps(raw))
        return outcome != "denied"

    def factory(profile):
        first = not created
        if first:
            call = (
                ToolCall("acquire", "tool_authoring", artifact_args)
                if kind == "artifact_authoring"
                else ToolCall("acquire", "request_tool", {"name": name, "reason": "Test"})
            )
        else:
            call = ToolCall("execute", name, {"command": "printf executed"})
        provider = MockProvider(
            (
                [
                    ProviderResponse(
                        [ToolCall("prepare", "tool_authoring", {"name": name})], "tool_use"
                    )
                ]
                if first and kind == "artifact_authoring"
                else []
            )
            + [
                ProviderResponse([call], "tool_use"),
                ProviderResponse([TextBlock("done")], "end_turn"),
            ]
        )
        agent = Agent(
            provider,
            ToolRegistry(profile.tools),
            AlwaysDeny() if not first and outcome in {"saved", "existing"} else AlwaysAllow(),
            ToolContext(
                tmp_path,
                artifact_root=catalog.artifact_root,
                tool_catalog=catalog,
                approve_artifact_preparation=AsyncMock(return_value=True),
                approve_artifact_creation=AsyncMock(return_value=True),
                persist_tool_access=make_profile_tool_access_persister(
                    path,
                    profile.name,
                    approve,
                    catalog,
                    report_error=errors.append,
                    on_saved=lambda tool_name: manager.grant_tool_access(
                        profile.name, catalog[tool_name]
                    ),
                ),
            ),
        )
        created.append(agent)
        return agent

    manager = SubagentManager(factory, profiles)
    handle = manager.get("lab")
    delegation = DelegationTool("lab", "Lab")
    delegation.bind(manager)
    assert await handle.run("Acquire") == "done"
    assert created[0].tools.get(name) is not None
    saved = outcome in {"saved", "existing"}
    acquisition = json.loads(created[0].provider.calls[-1][0][-1].content[0].content)
    saved_note = "Access was saved" if kind == "artifact_authoring" else "also saved"
    assert (saved_note in acquisition["message"]) == saved
    assert (name in [t.definition.name for t in manager.tools_for_profile("lab")]) == saved
    assert name not in [t.definition.name for t in manager.tools_for_profile("other")]
    assert manager.profiles["lab"].system_prompt == "Test tools"
    assert bool(errors) == (outcome == "failed")
    assert len(approvals) == (0 if outcome == "existing" else 1)
    stored = json.loads(path.read_text())["profiles"]["lab"]
    field = "tools" if kind == "native" else "tool_artifacts"
    assert (name in stored.get(field, [])) == saved
    assert name not in stored.get("tool_artifacts" if kind == "native" else "tools", [])
    if outcome in {"denied", "failed"}:
        assert "tool_artifacts" not in stored

    # Both a pre-save handle and a pre-bound delegation tool resolve the update.
    assert await handle.run("Execute") == "done"
    result = await delegation.execute(ToolContext(tmp_path), {"task": "Execute again"})
    assert not result.is_error
    assert len(created) == 3
    for agent in created[1:]:
        assert (agent.tools.get(name) is not None) == saved
        tool_result = agent.provider.calls[-1][0][-1].content[0]
        assert tool_result.is_error
        if saved:
            assert json.loads(tool_result.content)["status"] == "access_denied"
        else:
            assert "unknown tool" in tool_result.content
    restarted = load_profiles(path, ToolCatalog(load_tools(TOOLS), catalog.artifact_root))
    assigned = {t.definition.name for t in restarted["lab"].tools}
    assigned.update(restarted["lab"].tool_artifacts)
    assert (name in assigned) == saved


async def test_concurrent_saves_merge_and_cached_agent_syncs(tmp_path):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    result = await create(
        catalog,
        ToolContext(tmp_path, artifact_root=catalog.artifact_root, tool_catalog=catalog),
        {
            "name": "runtime_helper",
            "description": "Helper",
            "input_schema": {"type": "object"},
            "files": {"run.py": 'print("ok")'},
            "command": [sys.executable, "run.py"],
        },
    )
    assert not result.is_error
    path, profiles = setup_profiles(tmp_path, catalog, reuse=True)
    created = []

    def factory(profile):
        agent = Agent(
            MockProvider([ProviderResponse([TextBlock("done")], "end_turn")] * 2),
            ToolRegistry(profile.tools),
            AlwaysAllow(),
            ToolContext(tmp_path),
        )
        created.append(agent)
        return agent

    manager = SubagentManager(factory, profiles)
    handle = manager.get("lab")
    await handle.run("Initialize cached agent")
    # Unsaved access retains its existing lifetime in a reused session.
    created[0].tools.register(catalog["write_file"])
    barrier = asyncio.Barrier(2)

    async def approve(_definition, _arguments):
        await barrier.wait()
        return True

    persist = make_profile_tool_access_persister(
        path,
        "lab",
        approve,
        catalog,
        on_saved=lambda name: manager.grant_tool_access("lab", catalog[name]),
    )
    assert await asyncio.gather(persist("bash"), persist("runtime_helper")) == [True, True]
    await handle.run("Use saved grants")
    assert len(created) == 1
    assert {"bash", "runtime_helper", "write_file"} <= {
        item.name for item in created[0].tools.definitions()
    }
    assert {"bash", "runtime_helper"} <= {
        item.definition.name for item in manager.profiles["lab"].tools
    }
    stored = json.loads(path.read_text())["profiles"]["lab"]
    assert stored["tools"] == ["bash"]
    assert stored["tool_artifacts"] == ["runtime_helper"]
    assert "write_file" not in {item.definition.name for item in manager.profiles["lab"].tools}
    assert "bash" not in {item.definition.name for item in manager.profiles["other"].tools}
