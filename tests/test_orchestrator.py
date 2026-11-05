import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.agent.loop import NullEventSink
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolResult
from harness.commands import CommandContext, build_default_commands
from harness.compaction import SlidingWindow
from harness.mcp import MCPConfigStore, MCPServerConfig, MCPToolManager
from harness.orchestrator import load_orchestrator
from harness.provider import MockProvider
from harness.skills import load_skills
from harness.tools.loader import load_tools

TOOLS = Path(__file__).parents[1] / "tools.json"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "tools.json").write_text(TOOLS.read_text())
    for name in ("route", "child", "orphan"):
        directory = tmp_path / ".agents" / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} workflow\n---\nFULL {name} INSTRUCTIONS\n"
        )
    (tmp_path / "subagents.json").write_text(
        json.dumps(
            {
                "profiles": {
                    name: {
                        "description": name,
                        "system_prompt": f"Role {name}",
                        "tools": ["read_file"],
                        "skills": ["child"],
                        "permission_mode": "inherit",
                        "max_turns": 7,
                    }
                    for name in ("alpha", "beta")
                }
            }
        )
    )
    return tmp_path


def config(workspace, **changes):
    raw = {"tools": ["delegate_alpha"], "skills": [], "mcp_servers": []}
    raw.update(changes)
    (workspace / "orchestrator.json").write_text(json.dumps(raw))
    return raw


def load(workspace, *, memory_enabled=True):
    return load_orchestrator(
        workspace / "orchestrator.json",
        delegation_names=("delegate_alpha", "delegate_beta"),
        native_tools=load_tools(workspace / "tools.json"),
        skills=load_skills(workspace / "skills.json", workspace),
        memory_enabled=memory_enabled,
    )


@pytest.mark.parametrize("field", ["tools", "skills", "mcp_servers"])
def test_required_fields_are_not_silently_restored(workspace, field):
    raw = config(workspace)
    del raw[field]
    (workspace / "orchestrator.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=f"orchestrator.json {field}"):
        load(workspace)


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"tools": []}, "tools"),
        ({"tools": ["delegate_missing"]}, "tools"),
        *[
            ({"tools": ["delegate_alpha", name]}, "tools")
            for name in (
                "read_file",
                "bash",
                "request_tool",
                "tool_authoring",
                "artifact",
                "mcp_docs_search",
            )
        ],
        ({"tools": ["delegate_alpha", "delegate_alpha"]}, "tools"),
        ({"skills": ["missing"]}, "skills"),
        ({"skills": ["route"]}, "skills"),
        ({"skills": {}}, "skills"),
        ({"mcp_servers": ["*", "docs"]}, "mcp_servers"),
        ({"mcp_servers": ["Docs"]}, "mcp_servers"),
        ({"mcp_servers": [1]}, "mcp_servers"),
        ({"mcp_servers": ["docs", "docs"]}, "mcp_servers"),
        ({"extra": True}, "unknown fields"),
        *[({"max_turns": value}, "max_turns") for value in (0, -1, True, 1.5)],
    ],
)
def test_strict_config_rejects_invalid_resources_and_values(workspace, changes, field):
    config(workspace, **changes)
    with pytest.raises(ValueError, match=f"orchestrator.json .*{field}"):
        load(workspace)


def test_missing_file_only_uses_legacy_defaults(workspace):
    loaded = load(workspace)
    assert not loaded.explicit and "legacy defaults" in loaded.describe()
    assert set(loaded.tools) == {
        "delegate_alpha",
        "delegate_beta",
        "recall_experience",
        "record_experience",
    }
    assert loaded.skills == () and loaded.mcp_servers == ("*",)
    assert load(workspace, memory_enabled=False).tools == ("delegate_alpha", "delegate_beta")
    (workspace / "orchestrator.json").write_text("{")
    with pytest.raises(ValueError, match="orchestrator.json"):
        load(workspace)


@pytest.mark.parametrize("replacement", [None, "read_file"])
def test_activation_requires_real_installed_handler(workspace, replacement):
    config(workspace, tools=["delegate_alpha", "activate_skill"], skills=["route"])
    raw = json.loads((workspace / "tools.json").read_text())
    if replacement is None:
        del raw["tools"]["activate_skill"]
    else:
        raw["tools"]["activate_skill"]["handler"] = replacement
    (workspace / "tools.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="activate_skill handler in tools.json"):
        load(workspace)


async def boot(workspace, monkeypatch, provider=None, *, allow_tools=True):
    import harness.main as main_module

    root = provider or MockProvider([ProviderResponse([TextBlock("Done")], "end_turn")])
    root.system = "Stable root base"
    monkeypatch.setattr(main_module, "build_provider", lambda *_args: root)
    captured = []

    async def capture(agent, *_args, **_kwargs):
        captured.append(agent)
        return 0

    monkeypatch.setattr(main_module, "run_once", capture)
    args = ["--working-dir", str(workspace), "--prompt", "Test", "--output", "json"]
    if allow_tools:
        args.append("--allow-tools")
    assert await main_module.main(args) == 0
    return captured[0]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "memory_tools",
    [[], ["recall_experience"], ["record_experience"], ["recall_experience", "record_experience"]],
)
async def test_root_registry_is_authoritative_and_memory_controls_independent(
    workspace, monkeypatch, enabled, memory_tools
):
    config(workspace, tools=["delegate_alpha", *memory_tools], max_turns=3)
    (workspace / "memory.json").write_text(json.dumps({"enabled": enabled}))
    agent = await boot(workspace, monkeypatch)
    assert set(agent.tools.names()) == {"delegate_alpha", *(memory_tools if enabled else [])}
    assert agent.max_turns == 3
    assert (agent.after_tool_batch is not None) == (enabled and "recall_experience" in memory_tools)
    assert agent.context.tool_catalog is None and agent.context.allowed_skills == ()
    for name in ("delegate_beta", "read_file", "request_tool"):
        result = await agent.tools.execute(agent.context, name, {})
        assert result.is_error and "unknown tool" in result.content
    manager = agent.tools.get("delegate_alpha")._manager
    assert manager.profiles["beta"].max_turns == 7
    assert "read_file" in {tool.definition.name for tool in manager.profiles["beta"].tools}
    context = CommandContext(
        agent,
        agent.provider,
        agent.tools,
        subagents=manager,
        orchestrator=load(workspace, memory_enabled=enabled),
    )
    for command in ("/agents", "/tools"):
        text = build_default_commands().dispatch(command, context).text
        assert "root configuration: orchestrator.json" in text
        assert "MCP servers: none" in text
        if memory_tools and not enabled:
            assert f"inactive tools (memory disabled): {', '.join(memory_tools)}" in text


@pytest.mark.parametrize("allowed", [False, True])
async def test_root_skill_permission_scope_and_full_compaction_protection(
    workspace, monkeypatch, allowed
):
    config(workspace, tools=["delegate_alpha", "activate_skill"], skills=["route"])
    provider = MockProvider(
        [
            ProviderResponse(
                [
                    ToolCall("one", "activate_skill", {"name": "route"}),
                    ToolCall("bad", "activate_skill", {"name": "child"}),
                ],
                "tool_use",
            ),
            ProviderResponse([ToolCall("again", "activate_skill", {"name": "route"})], "tool_use"),
            ProviderResponse([TextBlock("Done")], "end_turn"),
        ]
    )
    agent = await boot(workspace, monkeypatch, provider, allow_tools=allowed)
    assert "route: route workflow" in provider.system
    assert "child workflow" not in provider.system and "FULL" not in provider.system
    before = provider.system
    assert before == "Stable root base\n\n" + agent.context.skill_catalog.catalog_prompt()
    assert "Return control to the root" not in before
    assert "Capability recovery:" not in before
    await agent.send("Use route")
    protected = [message for message in agent.messages if message.kind == "skill_activation"]
    assert len(protected) == int(allowed)
    results = {
        block.tool_use_id: block
        for message in agent.messages
        for block in message.content
        if isinstance(block, ToolResult)
    }
    assert results["one"].is_error == (not allowed)
    assert results["bad"].is_error
    assert set(agent.tools.names()) == {"delegate_alpha", "activate_skill"}
    if allowed:
        full = protected[0].content[0].text
        assert "FULL route INSTRUCTIONS" in full
        agent.compaction.strategy = SlidingWindow(keep_last=1)
        for _ in range(3):
            agent.messages, _ = await agent.compaction.run(agent.messages)
        remaining = [message for message in agent.messages if message.kind == "skill_activation"]
        assert len(remaining) == 1 and remaining[0].content[0].text == full
    assert provider.system == before


@pytest.mark.parametrize(
    "selection,expected",
    [((), []), (("docs",), ["docs"]), (("*",), ["docs", "docs_extra"]), (("private",), [])],
)
async def test_mcp_allowlist_filters_before_discovery_and_cached_visibility(
    workspace, selection, expected
):
    store = MCPConfigStore(workspace / "mcp.json")
    for name in ("docs", "docs_extra", "private"):
        store.add(
            MCPServerConfig(
                name,
                f"https://{name}.example/mcp",
                "agent" if name == "private" else "global",
                "alpha" if name == "private" else None,
            )
        )
    manager = MCPToolManager(store, NullEventSink(), server_allowlists={"root": selection})
    calls = []

    class Client:
        def __init__(self, url):
            calls.append(url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search", description="Search", input_schema={"type": "object"}
                    )
                ]
            )

    manager._client_factory = Client
    root_tools = await manager.tools_for("root")
    assert len(calls) == len(expected)
    assert {tool.definition.name for tool in root_tools} == {
        f"mcp_{name}_search" for name in expected
    }
    await manager.tools_for("alpha")  # Warm every cache through the child.
    assert len(calls) == 3
    assert manager.cached_tools_for("root") == root_tools
    assert [name for name, _ in manager.discovery_status("root")] == expected
    assert await manager.tools_for("root") is root_tools
    manager.unregister("docs")
    assert all(
        tool.definition.name != "mcp_docs_search" for tool in await manager.tools_for("root")
    )
    assert len(calls) == 3


@pytest.mark.parametrize("collision", [False, True])
async def test_main_refreshes_mcp_object_when_same_name_changes_source(
    workspace, monkeypatch, collision
):
    config(workspace, mcp_servers=["*"])
    store = MCPConfigStore(workspace / "mcp.json")
    old_name, remote = ("a", "b_c") if collision else ("docs", "search")
    store.add(MCPServerConfig(old_name, "https://old.example/mcp", "global"))

    class Client:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def list_tools(self):
            name = "c" if collision and "new" in self.url else remote
            return SimpleNamespace(tools=[SimpleNamespace(name=name, description="Search")])

        async def call_tool(self, *_args):
            return SimpleNamespace(content=[SimpleNamespace(text=self.url)])

    monkeypatch.setattr(
        MCPToolManager, "_client_factory_for", lambda self, url, auth=None: Client(url)
    )
    agent = await boot(workspace, monkeypatch)
    await agent.before_send()
    name = f"mcp_{old_name}_{remote}"
    old = agent.tools.get(name)
    assert old.url == "https://old.example/mcp"
    store.remove(old_name)
    if collision:
        # The old server still exists, but is no longer assigned to the root.
        store.add(MCPServerConfig(old_name, "https://old.example/mcp", "agent", "alpha"))
    store.add(
        MCPServerConfig("a_b" if collision else old_name, "https://new.example/mcp", "global")
    )
    await agent.before_send()
    assert agent.tools.get(name) is not old
    result = await agent.tools.execute(agent.context, name, {})
    assert result.content == "https://new.example/mcp"


async def test_root_skill_catalog_and_full_activation_survive_model_switch(workspace, monkeypatch):
    from contextlib import nullcontext

    import harness.main as main_module
    import harness.ui.interactive as interactive
    import harness.ui.prompt_toolkit_rich as ui
    from harness.model_catalog import ModelCatalog, ModelChoice

    config(workspace, tools=["delegate_alpha", "activate_skill"], skills=["route"])
    initial = ModelChoice("mock/initial", "openai", "initial", "Initial", {})
    changed = ModelChoice("mock/changed", "openai", "changed", "Changed", {})
    monkeypatch.setattr(ModelCatalog, "load", lambda _path: ModelCatalog((initial, changed)))
    startup = MockProvider(
        [
            ProviderResponse([ToolCall("skill", "activate_skill", {"name": "route"})], "tool_use"),
            ProviderResponse([TextBlock("Activated")], "end_turn"),
        ]
    )
    startup.system = "Root instructions and AGENTS context"
    switched = MockProvider([ProviderResponse([TextBlock("Done")], "end_turn")])

    async def choose(*_args):
        return startup, initial

    monkeypatch.setattr(main_module, "choose_startup_model", choose)
    monkeypatch.setattr(main_module, "build_provider_for_choice", lambda _choice: switched)

    class UI:
        def write(self, _text):
            pass

        def suspend(self):
            return nullcontext()

        async def emit(self, _event):
            pass

        async def approve(self, *_args):
            return True

    monkeypatch.setattr(ui, "create_ui", lambda _path: (UI(), UI()))

    async def run(_self, agent, _commands, context):
        before = agent.provider.system
        assert (
            before
            == "Root instructions and AGENTS context\n\n"
            + agent.context.skill_catalog.catalog_prompt()
        )
        await agent.send("Activate route")
        full = next(message for message in agent.messages if message.kind == "skill_activation")
        context.select_model(changed.key)
        assert switched.system == before and switched.system.count("## Available skills") == 1
        assert full in agent.messages and "FULL route INSTRUCTIONS" in full.content[0].text
        assert set(agent.tools.names()) == {"delegate_alpha", "activate_skill"}
        await agent.send("Continue")
        assert switched.system == before

    monkeypatch.setattr(interactive.InteractiveRunner, "run", run)
    assert await main_module.main(["--working-dir", str(workspace)]) == 0
