import json
import sys
from pathlib import Path

import pytest

from harness.agent import Agent
from harness.agent.loop import ToolExecutionEvent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolDefinition, ToolResult
from harness.permissions import AlwaysDeny, PermissionDecision
from harness.provider import MockProvider
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
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
    enabled = provider.calls[1][0][-1].content[0]
    assert enabled.tool_use_id == "request-1"
    assert json.loads(enabled.content)["status"] == "enabled"
    assert provider.calls[2][0][-1].content[0].content == "ran pytest"


async def test_tool_authoring_creates_and_activates_user_artifact(tmp_path, monkeypatch):
    tool_authoring = next(
        tool for tool in load_tools(TOOLS_PATH) if tool.definition.name == "tool_authoring"
    )
    registry = ToolRegistry()
    context = ToolContext(tmp_path, artifact_root=tmp_path / "artifacts", tool_registry=registry)

    def forbidden(*args, **kwargs):
        pytest.fail("initial authoring must not execute commands")

    with monkeypatch.context() as patch:
        patch.setattr("harness.tools.artifacts.run_process", forbidden)
        patch.setattr("asyncio.create_subprocess_exec", forbidden)
        patch.setattr("asyncio.create_subprocess_shell", forbidden)
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
    assert json.loads(result.content)["status"] == "created"
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
    authoring = next(
        tool for tool in load_tools(TOOLS_PATH) if tool.definition.name == "tool_authoring"
    )
    before = registry.definitions()
    result = await registry.execute(
        ToolContext(tmp_path, tool_registry=registry, tool_catalog={"tool_authoring": authoring}),
        "ask_for_tool",
        {"name": "missing", "reason": "required capability"},
    )
    payload = json.loads(result.content)
    assert payload["status"] == "not_installed"
    assert "ask_for_tool for tool_authoring" in payload["next_step"]
    assert registry.definitions() == before


@pytest.mark.parametrize("plain_mapping", [False, True])
async def test_discovery_lists_tools_without_grants_execution_or_persistence(
    tmp_path, plain_mapping
):
    native = load_tools(TOOLS_PATH)
    artifact = tmp_path / "artifacts" / "example"
    artifact.mkdir(parents=True)
    (artifact / "manifest.json").write_text(
        json.dumps(
            {
                "description": "Example artifact",
                "input_schema": {"type": "object"},
                "command": [sys.executable, "run.py"],
            }
        )
    )
    (artifact / "run.py").write_text('raise AssertionError("must not run")')
    catalog = ToolCatalog(native, artifact.parent)
    if plain_mapping:
        catalog = {name: catalog[name] for name in catalog}
    else:
        for name, contents in (("broken", "{"), ("invalid", "[]")):
            directory = artifact.parent / name
            directory.mkdir()
            (directory / "manifest.json").write_text(contents)
    registry = ToolRegistry([catalog["read_file"]])

    async def unexpected_persist(_name):
        pytest.fail("discovery must not persist access")

    result = await catalog["list_available_tools"].execute(
        ToolContext(
            tmp_path,
            tool_registry=registry,
            tool_catalog=catalog,
            persist_tool_access=unexpected_persist,
        ),
        {},
    )

    payload = json.loads(result.content)
    entries = {entry["name"]: entry for entry in payload["tools"]}
    assert not result.is_error
    assert entries["read_file"]["access"] == "available"
    assert entries["bash"]["access"] == "requires_approval"
    assert entries["example"] == {
        "name": "example",
        "description": "Example artifact",
        "access": "requires_approval",
    }
    assert {entry["name"] for entry in payload["errors"]} == (
        set() if plain_mapping else {"broken", "invalid"}
    )
    assert [definition.name for definition in registry.definitions()] == ["read_file"]


@pytest.mark.parametrize("with_alias", [False, True])
async def test_discovery_guidance_does_not_invent_a_request_control(tmp_path, with_alias):
    path = tmp_path / "tools.json"
    configs = json.loads(TOOLS_PATH.read_text())["tools"]
    selected = {"list_available_tools": configs["list_available_tools"]}
    if with_alias:
        selected["ask_for_tool"] = configs["request_tool"]
    path.write_text(json.dumps({"tools": selected}))
    tools = load_tools(path)
    registry = ToolRegistry(tools)
    before = registry.definitions()

    result = await registry.execute(
        ToolContext(
            tmp_path,
            tool_registry=registry,
            tool_catalog={tool.definition.name: tool for tool in tools},
        ),
        "list_available_tools",
        {},
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert "request_tool" not in payload["next_step"]
    assert "available tool-access request control" in payload["next_step"]
    assert "if no request control is available, report the limitation" in payload["next_step"]
    assert {tool["name"] for tool in payload["tools"]} == (
        {"list_available_tools", "ask_for_tool"} if with_alias else {"list_available_tools"}
    )
    assert registry.definitions() == before


async def test_discovery_enumeration_failure_is_structured(tmp_path, monkeypatch):
    catalog = ToolCatalog(load_tools(TOOLS_PATH), tmp_path / "artifacts")
    catalog.artifact_root.mkdir()
    registry = ToolRegistry([catalog["list_available_tools"]])

    def unreadable(_path):
        raise PermissionError("artifact directory is unreadable")

    monkeypatch.setattr(Path, "iterdir", unreadable)
    result = await registry.execute(
        ToolContext(tmp_path, tool_registry=registry, tool_catalog=catalog),
        "list_available_tools",
        {},
    )
    payload = json.loads(result.content)
    assert result.is_error
    assert payload["status"] == "discovery_failed"
    assert "unreadable" in payload["message"]
    assert "do not infer that tools are missing" in payload["next_step"]
    assert [definition.name for definition in registry.definitions()] == ["list_available_tools"]


@pytest.mark.parametrize("authoring_access", ["absent", "unassigned", "available"])
async def test_missing_request_is_structured_and_never_creates(tmp_path, authoring_access):
    tools = {tool.definition.name: tool for tool in load_tools(TOOLS_PATH)}
    registry = ToolRegistry([tools["tool_authoring"]] if authoring_access == "available" else [])
    catalog = tools if authoring_access != "absent" else {}
    before = registry.definitions()
    result = await tools["request_tool"].execute(
        ToolContext(
            tmp_path,
            tool_registry=registry,
            tool_catalog=catalog,
            artifact_root=tmp_path / "artifacts",
        ),
        {"name": "missing", "reason": "required step"},
    )

    payload = json.loads(result.content)
    assert result.is_error
    assert payload["status"] == "not_installed"
    assert "not installed in this harness" in payload["message"]
    assert ("tool_authoring" in payload["next_step"]) == (authoring_access != "absent")
    if authoring_access == "unassigned":
        assert "request_tool for tool_authoring" in payload["next_step"]
    assert not (tmp_path / "artifacts").exists()
    assert registry.definitions() == before


async def test_denied_request_is_structured_without_execution_grants_or_retry(tmp_path):
    tools = {tool.definition.name: tool for tool in load_tools(TOOLS_PATH)}
    provider = MockProvider(
        [
            ProviderResponse(
                [ToolCall("denied", "request_tool", {"name": "bash", "reason": "tests"})],
                "tool_use",
            ),
            ProviderResponse([TextBlock("Blocked")], "end_turn"),
        ]
    )
    events = []

    class Collector:
        async def emit(self, event):
            events.append(event)

    async def unexpected_persist(_name):
        pytest.fail("denial must not persist access")

    registry = ToolRegistry([tools["request_tool"]])
    agent = Agent(
        provider,
        registry,
        AlwaysDeny(),
        ToolContext(
            tmp_path,
            tool_catalog=tools,
            persist_tool_access=unexpected_persist,
        ),
        events=Collector(),
    )
    assert await agent.send("Run tests") == "Blocked"
    result = provider.calls[1][0][-1].content[0]
    assert result.is_error and result.tool_use_id == "denied"
    payload = json.loads(result.content)
    assert payload["status"] == "access_denied"
    assert "do not repeat" in payload["next_step"]
    assert not any(isinstance(event, ToolExecutionEvent) for event in events)
    assert registry.get("bash") is None
    assert len(provider.calls) == 2


async def test_unsaved_session_grant_remains_available(tmp_path):
    tools = {tool.definition.name: tool for tool in load_tools(TOOLS_PATH)}
    registry = ToolRegistry()

    async def do_not_save(_name):
        return False

    context = ToolContext(
        tmp_path, tool_registry=registry, tool_catalog=tools, persist_tool_access=do_not_save
    )
    for status in ("enabled", "already_available"):
        result = await tools["request_tool"].execute(context, {"name": "bash", "reason": "tests"})
        assert json.loads(result.content)["status"] == status
        assert not result.is_error
        assert registry.get("bash") is tools["bash"]


@pytest.mark.parametrize(
    "case,status",
    [
        ("validation", "validation_error"),
        ("configuration", "configuration_error"),
        ("load", "existing_artifact_invalid"),
    ],
)
async def test_request_failures_are_structured(tmp_path, case, status):
    tools = {tool.definition.name: tool for tool in load_tools(TOOLS_PATH)}
    artifact = tmp_path / "artifacts" / "broken"
    artifact.mkdir(parents=True)
    (artifact / "manifest.json").write_text("{")
    context = ToolContext(
        tmp_path,
        tool_registry=ToolRegistry(),
        tool_catalog=ToolCatalog(tools.values(), artifact.parent),
    )
    if case == "configuration":
        context = ToolContext(tmp_path)
    arguments = {"name": "broken", "reason": "required"} if case != "validation" else {}
    result = await tools["request_tool"].execute(context, arguments)
    assert result.is_error
    assert json.loads(result.content)["status"] == status


async def test_authoring_invalid_manifest_collision_is_structured_and_preserved(tmp_path):
    catalog = ToolCatalog(load_tools(TOOLS_PATH), tmp_path / "artifacts")
    artifact = catalog.artifact_root / "broken"
    artifact.mkdir(parents=True)
    manifest = artifact / "manifest.json"
    manifest.write_text("{")
    result = await catalog["tool_authoring"].execute(
        ToolContext(
            tmp_path,
            artifact_root=catalog.artifact_root,
            tool_catalog=catalog,
        ),
        {
            "name": "broken",
            "description": "Supplement",
            "input_schema": {"type": "object"},
            "files": {"run.py": "print('ok')"},
            "command": [sys.executable, "run.py"],
        },
    )
    assert result.is_error
    assert json.loads(result.content)["status"] == "existing_artifact_invalid"
    assert manifest.read_text() == "{"
    assert not (artifact / "run.py").exists()


@pytest.mark.parametrize("with_skill", [False, True])
async def test_main_assembles_shared_subagent_instructions(tmp_path, monkeypatch, with_skill):
    import harness.main as main_module
    from harness.subagents.instructions import CAPABILITY_INSTRUCTIONS

    (tmp_path / "tools.json").write_text(TOOLS_PATH.read_text())
    (tmp_path / "subagents.json").write_text(
        json.dumps(
            {
                "profiles": {
                    "explorer": {
                        "description": "Inspect",
                        "system_prompt": "Remain read-only.",
                        "permission_mode": "inherit",
                        "tools": ["read_file"],
                        "skills": ["example"] if with_skill else [],
                    }
                }
            }
        )
    )
    if with_skill:
        skill = tmp_path / ".agents" / "skills" / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: example\ndescription: Example workflow\n---\nRequired steps"
        )
    root = MockProvider(
        [
            ProviderResponse(
                [ToolCall("delegate", "delegate_explorer", {"task": "Inspect"})], "tool_use"
            ),
            ProviderResponse([TextBlock("Done")], "end_turn"),
        ]
    )
    child = MockProvider([ProviderResponse([TextBlock("Inspected")], "end_turn")])
    root.system = "Root prompt"
    child.system = "Provider default"
    providers = iter([root, child])
    monkeypatch.setattr(main_module, "build_provider", lambda *_args: next(providers))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    assert (
        await main_module.main(
            [
                "--working-dir",
                str(tmp_path),
                "--prompt",
                "Inspect",
                "--allow-tools",
            ]
        )
        == 0
    )
    assert child.system.startswith("Remain read-only.")
    assert CAPABILITY_INSTRUCTIONS.strip() in child.system
    assert ("Example workflow" in child.system) is with_skill
    assert root.system == "Root prompt"
    definitions = {definition.name for definition in child.calls[0][1]}
    assert {"list_available_tools", "request_tool"} <= definitions
