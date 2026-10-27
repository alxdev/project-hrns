import asyncio
import json
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolDefinition
from harness.permissions import AlwaysAsk, AlwaysDeny
from harness.provider import MockProvider
from harness.subagents import SubagentManager, SubagentProfile, load_profiles
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import load_tools
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


def artifact(tmp_path, description):
    return ArtifactTool(
        ToolDefinition("helper", description, {"type": "object"}),
        tmp_path / "helper",
        ("true",),
    )


async def test_refresh_recovers_assignments_and_reused_handles_without_new_grants(tmp_path):
    old = artifact(tmp_path, "Old")
    new = artifact(tmp_path, "New")
    unavailable = SubagentProfile(
        "unavailable",
        "Test",
        "Original prompt",
        reuse=True,
        tool_artifacts=("helper",),
        unavailable_artifacts={"helper": "Broken manifest"},
    )
    cached = replace(unavailable, name="cached", tools=(old,), unavailable_artifacts={})
    transient = replace(unavailable, name="transient", tool_artifacts=(), unavailable_artifacts={})
    other = replace(transient, name="other")
    created = {}

    def factory(profile):
        provider = MockProvider(
            [
                ProviderResponse([TextBlock("ready")], "end_turn"),
                ProviderResponse([ToolCall("execute", "helper", {})], "tool_use"),
                ProviderResponse([TextBlock("done")], "end_turn"),
            ]
        )
        agent = Agent(provider, ToolRegistry(profile.tools), AlwaysDeny(), ToolContext(tmp_path))
        created[profile.name] = agent
        return agent

    manager = SubagentManager(factory, {p.name: p for p in (unavailable, cached, transient, other)})
    handle = manager.get("unavailable")
    for name in manager.profiles:
        await manager.get(name).run("Initialize")
    created["transient"].tools.register(old)
    assert "Broken manifest" in manager.describe()
    manager.refresh_artifact_access(new)
    for name in ("unavailable", "cached"):
        assert manager.profiles[name].tools == (new,)
        assert manager.profiles[name].tool_artifacts == ("helper",)
        assert manager.profiles[name].unavailable_artifacts == {}
        assert created[name].tools.get("helper") is new
        assert manager.profiles[name].system_prompt == "Original prompt"
    assert created["transient"].tools.get("helper") is new
    assert manager.profiles["transient"].tools == ()
    assert manager.profiles["transient"].tool_artifacts == ()
    assert manager.tools_for_profile("transient") == (new,)
    assert "tools: helper" in manager.describe()
    assert created["other"].tools.get("helper") is None
    assert manager.profiles["other"].tools == ()
    assert "Broken manifest" not in manager.describe()
    assert await handle.run("Execute") == "done"
    result = created["unavailable"].provider.calls[-1][0][-1].content[0]
    assert result.is_error
    assert json.loads(result.content)["status"] == "access_denied"
    assert (
        next(
            d for d in created["unavailable"].provider.calls[-1][1] if d.name == "helper"
        ).description
        == "New"
    )


def test_saved_artifact_remembers_intent_for_later_refresh(tmp_path):
    profile = SubagentProfile("lab", "Test", "Test")
    manager = SubagentManager(lambda _: pytest.fail("No delegation"), {"lab": profile})
    manager.grant_tool_access("lab", artifact(tmp_path, "Saved"))
    new = artifact(tmp_path, "Replacement")
    manager.refresh_artifact_access(new)
    assert manager.profiles["lab"].tool_artifacts == ("helper",)
    assert manager.profiles["lab"].tools == (new,)


@pytest.mark.parametrize("native_tools,expected", [(["read_file"], 0), (["missing"], 2)])
async def test_startup_skips_only_invalid_artifacts(
    tmp_path, monkeypatch, capsys, native_tools, expected
):
    import harness.main as main_module
    from harness.config import HarnessConfig

    artifact_root = tmp_path / "data" / "tool-artifacts"
    broken = artifact_root / "broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{")
    (tmp_path / "tools.json").write_text(TOOLS.read_text())
    profile_path = tmp_path / "subagents.json"
    profile_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "lab": {
                        "description": "Test",
                        "system_prompt": "Test",
                        "permission_mode": "inherit",
                        "tools": native_tools,
                        "tool_artifacts": ["broken"],
                    }
                }
            }
        )
    )
    original = profile_path.read_bytes()
    root = MockProvider(
        [
            ProviderResponse(
                [ToolCall("delegate", "delegate_lab", {"task": "Inspect"})], "tool_use"
            ),
            ProviderResponse([TextBlock("Done")], "end_turn"),
        ]
    )
    child = MockProvider([ProviderResponse([TextBlock("Inspected")], "end_turn")])
    root.system = child.system = "Test"
    providers = iter([root, child])
    monkeypatch.setattr(main_module, "build_provider", lambda *_: next(providers))
    monkeypatch.setattr(HarnessConfig, "data_directory", property(lambda _: tmp_path / "data"))
    assert (
        await main_module.main(
            ["--working-dir", str(tmp_path), "--prompt", "Inspect", "--allow-tools"]
        )
        == expected
    )
    assert profile_path.read_bytes() == original
    output = capsys.readouterr()
    if expected == 0:
        assert "invalid tool artifact" in output.err
        assert "broken" in output.err
        assert "invalid manifest JSON" in output.err
        assert {d.name for d in child.calls[0][1]} >= {"read_file", "request_tool"}
        assert "broken" not in {d.name for d in child.calls[0][1]}
        profiles = load_profiles(profile_path, ToolCatalog(load_tools(TOOLS), artifact_root))
        assert profiles["lab"].unavailable_artifacts == {}
    else:
        assert "unknown tool 'missing'" in output.err
        assert not child.calls


@pytest.mark.parametrize("interactive,approved", [(False, False), (True, False), (True, True)])
async def test_main_replacement_callback_uses_explicit_suspended_approval(
    tmp_path, monkeypatch, interactive, approved
):
    import harness.main as main_module
    import harness.ui.interactive as interactive_module
    import harness.ui.prompt_toolkit_rich as ui_module
    from harness.config import HarnessConfig

    (tmp_path / "tools.json").write_text(TOOLS.read_text())
    path = tmp_path / "subagents.json"
    path.write_text(
        json.dumps(
            {
                "profiles": {
                    "lab": {
                        "description": "Test",
                        "system_prompt": "Test",
                        "permission_mode": "auto_allow",
                        "tools": ["tool_authoring"],
                        "tool_artifacts": ["helper"],
                    }
                }
            }
        )
    )
    original = path.read_bytes()
    broken = tmp_path / "data" / "tool-artifacts" / "helper"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{")
    monkeypatch.setattr(HarnessConfig, "data_directory", property(lambda _: tmp_path / "data"))
    provider = MockProvider()
    provider.system = "Test"
    monkeypatch.setattr(main_module, "build_provider", lambda *_: provider)
    monkeypatch.setattr(main_module, "build_provider_for_choice", lambda *_: provider)
    approvals = []
    suspended = []

    class Output:
        def write(self, _text):
            pass

        def error(self, _text):
            pass

        @contextmanager
        def suspend(self):
            suspended.append(True)
            try:
                yield
            finally:
                suspended.pop()

    class Input:
        async def approve(self, definition, arguments):
            assert suspended
            approvals.append((definition, arguments))
            return approved

    monkeypatch.setattr(ui_module, "create_ui", lambda _: (Input(), Output()))

    async def choose(*_args):
        return provider, SimpleNamespace(provider="openai", key="test")

    monkeypatch.setattr(main_module, "choose_startup_model", choose)
    managers = []

    def manager_factory(*args, **kwargs):
        manager = SubagentManager(*args, **kwargs)
        managers.append(manager)
        return manager

    monkeypatch.setattr(main_module, "SubagentManager", manager_factory)

    async def verify():
        manager = managers[0]
        child = manager.factory(manager.profiles["lab"])
        context = child.context
        definition = ToolDefinition("replace_invalid_artifact", "Archive and replace", {})
        arguments = {"artifact": str(broken), "archive": str(broken.parent / "archive")}
        assert await context.approve_artifact_replacement(definition, arguments) is approved
        assert bool(approvals) is interactive
        if interactive:
            assert approvals == [(definition, arguments)]
        # Approval alone must not refresh a profile. The installer owns the success callback.
        assert manager.profiles["lab"].unavailable_artifacts == {}
        assert context.on_artifact_replaced is not None
        if approved:
            new = artifact(tmp_path, "Recreated")
            context.on_artifact_replaced(new)
            assert manager.profiles["lab"].tools[-1] is new
            assert not manager.profiles["lab"].unavailable_artifacts
        assert path.read_bytes() == original

    async def run_once(*_args, **_kwargs):
        await verify()
        return 0

    async def run(*_args):
        await verify()

    monkeypatch.setattr(main_module, "run_once", run_once)
    monkeypatch.setattr(interactive_module.InteractiveRunner, "run", run)
    argv = ["--working-dir", str(tmp_path)]
    if not interactive:
        argv += ["--prompt", "Recreate", "--allow-tools"]
    assert await main_module.main(argv) == 0


@pytest.mark.parametrize(
    "replacement_approved,execution_approved", [(True, True), (True, False), (False, True)]
)
async def test_real_recreation_recovers_startup_assignment_in_same_process(
    tmp_path, replacement_approved, execution_approved
):
    root = tmp_path / "artifacts"
    broken = root / "helper"
    broken.mkdir(parents=True)
    original_manifest = b'{"invalid":'
    (broken / "manifest.json").write_bytes(original_manifest)
    (broken / "original.txt").write_text("Keep original")
    catalog = ToolCatalog(load_tools(TOOLS), root)
    path = tmp_path / "subagents.json"
    path.write_text(
        json.dumps(
            {
                "profiles": {
                    "lab": {
                        "description": "Metrics",
                        "system_prompt": "Count text",
                        "tools": ["read_file"],
                        "tool_artifacts": ["helper"],
                        "permission_mode": "ask",
                        "reuse": True,
                    }
                }
            }
        )
    )
    original_profile = path.read_bytes()
    profiles = load_profiles(path, catalog)
    assert profiles["lab"].unavailable_artifacts == {}
    assert "helper" not in {t.definition.name for t in profiles["lab"].tools}
    text_input = {"text": "one two\nthree"}
    executions = []
    created = []

    async def approve_execution(definition, arguments):
        executions.append((definition.name, arguments))
        return execution_approved

    def factory(profile):
        provider = MockProvider(
            [
                ProviderResponse([TextBlock("ready")], "end_turn"),
                ProviderResponse([ToolCall("metrics", "helper", text_input)], "tool_use"),
                ProviderResponse([TextBlock("done")], "end_turn"),
            ]
        )
        agent = Agent(
            provider,
            ToolRegistry(profile.tools),
            AlwaysAsk(approve_execution),
            ToolContext(tmp_path),
        )
        created.append(agent)
        return agent

    manager = SubagentManager(factory, profiles)
    handle = manager.get("lab")
    await handle.run("Initialize before recovery")
    assert "helper" in manager.profiles["lab"].unavailable_artifacts
    assert "helper" not in {d.name for d in created[0].provider.calls[0][1]}
    approvals = []
    refreshed = []

    async def approve_replacement(definition, arguments):
        approvals.append((definition.name, arguments))
        assert arguments["path"] == str(broken)
        assert arguments["archive_path"] == str(root / "INVALID_helper")
        return replacement_approved

    def installed(tool):
        refreshed.append(tool)
        manager.refresh_artifact_access(tool)

    context = ToolContext(
        tmp_path,
        artifact_root=root,
        tool_catalog=catalog,
        tool_registry=ToolRegistry(),
        approve_artifact_replacement=approve_replacement,
        on_artifact_replaced=installed,
    )
    result = await catalog["tool_authoring"].execute(
        context,
        {
            "name": "helper",
            "replace_invalid": True,
            "description": "Count lines and words",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            "command": [sys.executable, "run.py"],
            "files": {
                "run.py": (
                    "import json, pathlib, sys\n"
                    "text = json.load(sys.stdin)['text']\n"
                    "pathlib.Path('executed').write_text('yes')\n"
                    "print(json.dumps({'line_count': len(text.splitlines()), "
                    "'word_count': len(text.split())}))\n"
                )
            },
        },
    )
    payload = json.loads(result.content)
    assert len(approvals) == 1
    assert bool(refreshed) is replacement_approved, payload
    assert not (tmp_path / "executed").exists()  # Authoring does not execute the artifact.
    assert path.read_bytes() == original_profile
    if replacement_approved:
        assert not result.is_error and payload["status"] == "recreated"
        archive = Path(payload["archive_path"])
        assert (archive / "manifest.json").read_bytes() == original_manifest
        assert (archive / "original.txt").read_text() == "Keep original"
        assert "INVALID_helper" not in catalog
        assert "INVALID_helper" not in list(catalog)
        assert not manager.profiles["lab"].unavailable_artifacts
    else:
        assert result.is_error and payload["status"] == "approval_denied"
        assert (broken / "manifest.json").read_bytes() == original_manifest
        assert not (root / "INVALID_helper").exists()
        assert "helper" in manager.profiles["lab"].unavailable_artifacts
    assert await handle.run("Count text") == "done"
    execution_result = created[0].provider.calls[-1][0][-1].content[0]
    assert executions == [("helper", text_input)]
    if replacement_approved:
        if execution_approved:
            assert not execution_result.is_error
            assert json.loads(execution_result.content) == {"line_count": 2, "word_count": 3}
        else:
            assert execution_result.is_error
            assert json.loads(execution_result.content)["status"] == "access_denied"
    else:
        assert execution_result.is_error and "unknown tool" in execution_result.content
    assert (tmp_path / "executed").exists() is (replacement_approved and execution_approved)
    assert len(created) == 1
    assert path.read_bytes() == original_profile


@pytest.mark.parametrize("assigned", [False, True])
async def test_refresh_updates_live_nonreused_agents_and_reports_session_access(tmp_path, assigned):
    old = artifact(tmp_path, "Old")
    new = replace(artifact(tmp_path, "New"), command=("replacement-command",))
    profile = SubagentProfile("lab", "Lab", "Test", tools=(old,) if assigned else ())
    started = asyncio.Event()
    resume = asyncio.Event()
    created = []

    async def respond(_messages, definitions):
        if not started.is_set():
            started.set()
            await resume.wait()
            return ProviderResponse([ToolCall("call", "helper", {})], "tool_use")
        assert next(d for d in definitions if d.name == "helper").description == "New"
        return ProviderResponse([TextBlock("done")], "end_turn")

    def factory(effective_profile):
        agent = Agent(
            MockProvider(responder=respond),
            ToolRegistry(effective_profile.tools),
            AlwaysDeny(),
            ToolContext(tmp_path),
        )
        if not assigned:
            agent.tools.register(old)
        created.append(agent)
        return agent

    manager = SubagentManager(factory, {"lab": profile})
    task = asyncio.create_task(manager.get("lab").run("Wait for replacement"))
    await asyncio.wait_for(started.wait(), 2)
    try:
        assert manager.tools_for_profile("lab") == (old,)
        manager.refresh_artifact_access(new)
        assert created[0].tools.get("helper") is new
        assert manager.tools_for_profile("lab") == (new,)
        assert "tools: helper" in manager.describe()
        assert bool(manager.profiles["lab"].tools) is assigned
        assert bool(manager.profiles["lab"].tool_artifacts) is assigned
    finally:
        resume.set()
        await task
    assert not manager._live_agents
    if not assigned:
        assert manager.tools_for_profile("lab") == ()
        assert manager.profiles["lab"].tools == ()


@pytest.mark.parametrize("cancel", [False, True])
async def test_live_agent_tracking_cleans_up_on_failure_and_cancellation(tmp_path, cancel):
    started = asyncio.Event()

    async def respond(_messages, _definitions):
        started.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("provider failed")

    profile = SubagentProfile("lab", "Lab", "Test")
    manager = SubagentManager(
        lambda p: Agent(
            MockProvider(responder=respond),
            ToolRegistry(p.tools),
            AlwaysDeny(),
            ToolContext(tmp_path),
        ),
        {"lab": profile},
    )
    task = asyncio.create_task(manager.get("lab").run("Run"))
    await asyncio.wait_for(started.wait(), 2)
    if cancel:
        assert manager._live_agents
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await task
    assert not manager._live_agents
