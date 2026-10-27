import json
import sys
from pathlib import Path

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall
from harness.permissions import AlwaysAllow, AlwaysDeny
from harness.provider import MockProvider
from harness.skills import Skill, SkillCatalog
from harness.subagents import load_profiles
from harness.tools.access import make_profile_tool_access_persister
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import load_artifact_tool, load_tools
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


def artifact_arguments():
    return {
        "name": "math_helper",
        "description": "Add numbers using a generated script.",
        "input_schema": {"type": "object"},
        "files": {
            "run.py": 'import json,sys\nargs=json.load(sys.stdin)\nprint(args["a"] + args["b"])\n'
        },
        "command": [sys.executable, "run.py"],
    }


async def test_builder_artifact_is_requestable_by_another_agent_and_reloadable(tmp_path):
    native = load_tools(TOOLS)
    catalog = ToolCatalog(native, tmp_path / "user" / "artifacts")
    builder = ToolRegistry([catalog["tool_authoring"]])
    result = await builder.execute(
        ToolContext(
            tmp_path,
            artifact_root=catalog.artifact_root,
            tool_registry=builder,
            tool_catalog=catalog,
        ),
        "tool_authoring",
        artifact_arguments(),
    )
    assert not result.is_error
    assert "math_helper" in catalog
    profiles_path = tmp_path / "subagents.json"
    profiles_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "coder": {
                        "description": "Code",
                        "system_prompt": "Code",
                        "tools": ["request_tool"],
                        "permission_mode": "inherit",
                    }
                }
            }
        )
    )
    approvals = []

    async def approve(_definition, arguments):
        approvals.append(arguments)
        return True

    provider = MockProvider(
        [
            ProviderResponse([ToolCall("ungranted", "math_helper", {"a": 2, "b": 3})], "tool_use"),
            ProviderResponse(
                [
                    ToolCall(
                        "request",
                        "request_tool",
                        {"name": "math_helper", "reason": "Use the created capability"},
                    )
                ],
                "tool_use",
            ),
            ProviderResponse([ToolCall("run", "math_helper", {"a": 2, "b": 3})], "tool_use"),
            ProviderResponse([TextBlock("5")], "end_turn"),
        ]
    )
    context = ToolContext(
        tmp_path,
        persist_tool_access=make_profile_tool_access_persister(
            profiles_path, "coder", approve, catalog
        ),
        tool_catalog=catalog,
    )
    coder = Agent(
        provider,
        ToolRegistry([catalog["request_tool"]]),
        AlwaysAllow(),
        context,
    )

    assert await coder.send("Use the builder tool") == "5"
    assert provider.calls[1][0][-1].content[0].is_error
    assert provider.calls[-1][0][-1].content[0].content.strip() == "5"
    assert approvals == [{"profile": "coder", "tool": "math_helper"}]
    # A fresh catalog and fresh profiles reproduce a new process startup.
    restarted = ToolCatalog(native, catalog.artifact_root)
    profiles = load_profiles(profiles_path, restarted)
    assert "math_helper" in profiles["coder"].tool_artifacts
    assert all(tool.definition.name != "math_helper" for tool in profiles["coder"].tools)
    restored_tool = restarted["math_helper"]
    assert (
        await restored_tool.execute(ToolContext(tmp_path), {"a": 3, "b": 4})
    ).content.strip() == "7"


async def test_denied_artifact_request_does_not_grant_access(tmp_path):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    context = ToolContext(tmp_path, artifact_root=catalog.artifact_root, tool_catalog=catalog)
    await catalog["tool_authoring"].execute(context, artifact_arguments())
    provider = MockProvider(
        [
            ProviderResponse(
                [ToolCall("request", "request_tool", {"name": "math_helper", "reason": "Need it"})],
                "tool_use",
            ),
            ProviderResponse([TextBlock("Denied")], "end_turn"),
        ]
    )
    registry = ToolRegistry([catalog["request_tool"]])
    agent = Agent(provider, registry, AlwaysDeny(), context)

    await agent.send("Try it")

    assert [definition.name for definition in registry.definitions()] == ["request_tool"]


async def test_artifact_runs_in_project_with_artifact_and_skill_paths(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("project data")
    skill_dir = tmp_path / "user" / "skills" / "example"
    skill_dir.mkdir(parents=True)
    manifest = skill_dir / "SKILL.md"
    manifest.write_text("Skill instructions")
    skills = SkillCatalog({"example": Skill("example", "Example", skill_dir, manifest)})
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "user" / "artifacts")
    context = ToolContext(
        workspace,
        artifact_root=catalog.artifact_root,
        skill_catalog=skills,
        allowed_skills=("example",),
    )
    arguments = artifact_arguments()
    arguments["files"] = {
        "run.py": (
            "import json,os,pathlib\n"
            'print(json.dumps({"cwd":os.getcwd(),'
            '"data":pathlib.Path("marker.txt").read_text(),'
            '"artifact":os.environ["HARNESS_ARTIFACT_DIR"],'
            '"workspace":os.environ["HARNESS_WORKSPACE_DIR"],'
            '"skills":json.loads(os.environ["HARNESS_SKILL_DIRS"])}))\n'
        )
    }
    result = await catalog["tool_authoring"].execute(context, arguments)
    assert not result.is_error

    output = json.loads((await catalog["math_helper"].execute(context, {})).content)

    assert Path(output["cwd"]) == workspace.resolve()
    assert output["data"] == "project data"
    assert Path(output["workspace"]) == workspace.resolve()
    assert Path(output["artifact"]) == catalog.artifact_root / "math_helper"
    assert output["skills"] == {"example": str(skill_dir.resolve())}


async def test_non_python_script_can_use_explicit_artifact_path(tmp_path):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    arguments = artifact_arguments()
    arguments["files"] = {"run.sh": 'printf "%s" "$HARNESS_WORKSPACE_DIR"\n'}
    arguments["command"] = ["sh", "{artifact_dir}/run.sh"]
    context = ToolContext(tmp_path, artifact_root=catalog.artifact_root)

    assert not (await catalog["tool_authoring"].execute(context, arguments)).is_error
    result = await catalog["math_helper"].execute(context, {})

    assert not result.is_error
    assert Path(result.content) == tmp_path.resolve()


def test_artifact_scripts_are_not_executed_during_discovery(tmp_path):
    root = tmp_path / "artifacts" / "example"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "description": "Example",
                "input_schema": {"type": "object"},
                "command": [sys.executable, "run.py"],
            }
        )
    )
    (root / "run.py").write_text('raise RuntimeError("Must not execute during discovery")\n')

    tool = load_artifact_tool(root.parent, "example")

    assert tool.definition.name == "example"


@pytest.mark.parametrize("save", [True, False])
async def test_authoring_persists_profile_access_only_with_separate_approval(tmp_path, save):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    profiles_path = tmp_path / "subagents.json"
    profiles_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "builder": {
                        "description": "Build",
                        "system_prompt": "Build",
                        "tools": ["tool_authoring"],
                        "permission_mode": "inherit",
                    }
                }
            }
        )
    )
    approvals = []

    async def approve(definition, arguments):
        approvals.append((definition.name, arguments))
        return save

    context = ToolContext(
        tmp_path,
        artifact_root=catalog.artifact_root,
        persist_tool_access=make_profile_tool_access_persister(
            profiles_path, "builder", approve, catalog
        ),
    )

    result = await catalog["tool_authoring"].execute(context, artifact_arguments())

    assert not result.is_error
    assert "math_helper" in catalog
    assert approvals == [("save_tool_access", {"profile": "builder", "tool": "math_helper"})]
    restored = load_profiles(profiles_path, ToolCatalog(load_tools(TOOLS), catalog.artifact_root))
    assert ("math_helper" in restored["builder"].tool_artifacts) is save


async def test_inline_command_arguments_are_not_required_to_be_paths(tmp_path):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    arguments = artifact_arguments()
    arguments["command"] = [sys.executable, "-c", "# " + "inline " * 1000 + '\nprint("ok")']
    context = ToolContext(tmp_path, artifact_root=catalog.artifact_root)

    assert not (await catalog["tool_authoring"].execute(context, arguments)).is_error
    result = await catalog["math_helper"].execute(context, {})

    assert not result.is_error
    assert result.content.strip() == "ok"


@pytest.mark.parametrize("kind", ["native", "artifact"])
async def test_authoring_rejects_installed_names_without_replacement(tmp_path, kind):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    registry = ToolRegistry()
    context = ToolContext(
        tmp_path,
        artifact_root=catalog.artifact_root,
        tool_catalog=catalog,
        tool_registry=registry,
    )
    arguments = artifact_arguments()
    if kind == "artifact":
        assert not (await catalog["tool_authoring"].execute(context, arguments)).is_error
        directory = catalog.artifact_root / "math_helper"
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
    else:
        arguments["name"] = "bash"
    name = arguments["name"]
    installed = catalog[name]
    registered = registry.get(name)
    arguments["files"] = {"replacement.py": "raise RuntimeError('replaced')"}

    result = await catalog["tool_authoring"].execute(context, arguments)

    assert result.is_error
    assert json.loads(result.content)["status"] == "already_installed"
    assert catalog[name] == installed
    assert registry.get(name) is registered
    if kind == "artifact":
        assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    else:
        assert not (catalog.artifact_root / name).exists()
