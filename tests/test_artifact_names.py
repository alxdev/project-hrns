import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from harness.api.types import ToolDefinition
from harness.subagents import SubagentManager, load_profiles
from harness.tools.access import make_profile_tool_access_persister
from harness.tools.artifact_validation import is_valid_artifact_name
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.handlers import list_available_tools, request_tool, tool_authoring
from harness.tools.loader import load_artifact_tool, load_tools
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


def proposal(name):
    return {
        "name": name,
        "description": name,
        "input_schema": {"type": "object"},
        "command": ["never-execute", "run.py"],
        "files": {"run.py": "raise AssertionError('must not execute')"},
    }


@pytest.fixture
def context(tmp_path):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    return ToolContext(
        tmp_path,
        artifact_root=catalog.artifact_root,
        tool_catalog=catalog,
        tool_registry=ToolRegistry(),
        approve_artifact_preparation=AsyncMock(return_value=True),
        approve_artifact_creation=AsyncMock(return_value=True),
        approve_artifact_replacement=AsyncMock(return_value=True),
    )


async def test_dashed_name_intent_install_cache_list_request_and_fresh_profile(context, tmp_path):
    name = "text-metrics"
    premature = await tool_authoring.execute(context, proposal(name))
    assert premature.is_error
    assert json.loads(premature.content)["status"] == "preparation_required"
    assert not context.artifact_root.exists()
    prepared = await tool_authoring.execute(context, {"name": name})
    assert json.loads(prepared.content)["status"] == "preparation_authorized"
    assert not context.artifact_root.exists()
    context.approve_artifact_preparation.assert_awaited_once()
    context.approve_artifact_creation.assert_not_awaited()

    installed = await tool_authoring.execute(context, proposal(name))
    assert not installed.is_error
    assert json.loads(installed.content)["status"] == "created"
    assert json.loads(installed.content)["execution_verified"] is False
    context.approve_artifact_creation.assert_awaited_once()
    assert not context.artifact_authoring_intents
    tool = context.tool_registry.get(name)
    assert context.tool_catalog[name] is tool  # Trusted insertion populated the cache.
    assert context.tool_catalog[name] is tool
    assert load_artifact_tool(context.artifact_root, name).definition.name == name

    listing = json.loads((await list_available_tools.execute(context, {})).content)
    assert next(item for item in listing["tools"] if item["name"] == name)["access"] == "available"

    # Only this temporary profile is written; never the repository's subagents.json.
    profile_path = tmp_path / "profiles.json"
    profile_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "coder": {
                        "description": "Code",
                        "system_prompt": "Code",
                        "tools": [],
                        "permission_mode": "inherit",
                    }
                }
            }
        )
    )
    approval = AsyncMock(return_value=True)
    requester = replace(
        context,
        tool_registry=ToolRegistry(),
        persist_tool_access=make_profile_tool_access_persister(
            profile_path, "coder", approval, context.tool_catalog
        ),
    )
    listing = json.loads((await list_available_tools.execute(requester, {})).content)
    assert next(item for item in listing["tools"] if item["name"] == name)["access"] == (
        "requires_approval"
    )
    result = await request_tool.execute(requester, {"name": name, "reason": "Count text"})
    assert not result.is_error
    assert json.loads(result.content)["status"] == "enabled"
    assert requester.tool_registry.get(name) is tool
    approval.assert_awaited_once()
    assert approval.call_args.args[1] == {"profile": "coder", "tool": name}
    fresh = ToolCatalog(load_tools(TOOLS), context.artifact_root)
    profiles = load_profiles(profile_path, fresh)
    assert profiles["coder"].tool_artifacts == (name,)
    factory = Mock(
        side_effect=lambda profile: Mock(
            tools=ToolRegistry(profile.tools), send=AsyncMock(return_value="ready")
        )
    )
    manager = SubagentManager(factory, profiles)
    await manager.delegate("coder", "Resolve saved artifacts")
    assert fresh[name] in factory.call_args.args[0].tools

    before = (tool.directory / "manifest.json").read_bytes()
    protected = await tool_authoring.execute(context, proposal(name))
    assert json.loads(protected.content)["status"] == "already_installed"
    assert (tool.directory / "manifest.json").read_bytes() == before
    context.approve_artifact_creation.assert_awaited_once()


async def test_dashed_recovery_preserves_archive_and_excludes_it_from_discovery(context):
    name = "text-metrics"
    directory = context.artifact_root / name
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text("{")
    (directory / "original.txt").write_text("preserve")
    invalid = await tool_authoring.execute(context, {"name": name})
    assert json.loads(invalid.content)["status"] == "existing_artifact_invalid"
    archived = await tool_authoring.execute(context, {"name": name, "replace_invalid": True})
    assert json.loads(archived.content)["status"] == "original_archived"
    context.approve_artifact_replacement.assert_awaited_once()
    context.approve_artifact_creation.assert_not_awaited()
    context.approve_artifact_preparation.assert_not_awaited()
    installed = await tool_authoring.execute(context, proposal(name))
    assert not installed.is_error
    assert json.loads(installed.content)["status"] == "created"
    archive = context.artifact_root / f"INVALID_{name}"
    assert (archive / "manifest.json").read_text() == "{"
    assert (archive / "original.txt").read_text() == "preserve"
    for catalog in (context.tool_catalog, ToolCatalog([], context.artifact_root)):
        assert list(catalog).count(name) == 1
        assert archive.name not in list(catalog)
        assert archive.name not in catalog
    with pytest.raises(ValueError):
        load_artifact_tool(context.artifact_root, archive.name)


async def test_dash_and_underscore_names_remain_distinct(context):
    for name in ("text-metrics", "text_metrics"):
        assert not (await tool_authoring.execute(context, {"name": name})).is_error
        assert not (await tool_authoring.execute(context, proposal(name))).is_error
    fresh = ToolCatalog([], context.artifact_root)
    assert list(fresh) == ["text-metrics", "text_metrics"]
    assert fresh["text-metrics"].directory != fresh["text_metrics"].directory
    for name in fresh:
        assert fresh[name].definition.name == name
        assert fresh[name].definition.description == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Upper-case",
        "INVALID_text-metrics",
        "-leading",
        "_leading",
        "1leading",
        ".",
        "..",
        "../escape",
        "a/b",
        "a\\b",
        "/absolute",
        "a:b",
        "a.b",
        "trailing- ",
        "trailing-.",
        "a\n",
        "a\0",
        "con",
        "nul",
        "aux",
        "com1",
        "lpt9",
    ],
)
async def test_invalid_names_rejected_by_authoring_loader_and_all_catalog_paths(context, name):
    assert not is_valid_artifact_name(name)
    result = await tool_authoring.execute(context, {"name": name})
    assert result.is_error
    assert json.loads(result.content)["status"] == "validation_error"
    context.approve_artifact_preparation.assert_not_awaited()
    context.approve_artifact_creation.assert_not_awaited()
    with pytest.raises(ValueError):
        load_artifact_tool(context.artifact_root, name)
    with pytest.raises(KeyError):
        context.tool_catalog[name]
    tool = ArtifactTool(
        ToolDefinition(name, "Invalid", {"type": "object"}),
        context.artifact_root / name,
        ("never-execute",),
    )
    with pytest.raises(ValueError):
        context.tool_catalog.cache_validated_artifact(tool)
    assert not context.artifact_root.exists()


def test_discovery_excludes_reserved_and_uppercase_directories(context):
    for name in ("con", "aux", "com1", "Upper-case", "INVALID_text-metrics", "text-metrics"):
        (context.artifact_root / name).mkdir(parents=True)
    assert list(ToolCatalog([], context.artifact_root)) == ["text-metrics"]


async def test_native_dashed_tool_protected_but_dashed_handler_module_rejected(context, tmp_path):
    config = tmp_path / "tools.json"
    definition = {"description": "Native", "input_schema": {}, "handler": "read_file"}
    config.write_text(json.dumps({"tools": {"native-tool": definition}}))
    native = load_tools(config)
    catalog = ToolCatalog(native, context.artifact_root)
    context = replace(context, tool_catalog=catalog)
    assert catalog["native-tool"] is native[0]
    result = await tool_authoring.execute(context, {"name": "native-tool"})
    assert json.loads(result.content)["status"] == "already_installed"
    context.approve_artifact_preparation.assert_not_awaited()
    assert not context.artifact_root.exists()
    config.write_text(
        json.dumps({"tools": {"native-tool": {**definition, "handler": "read-file"}}})
    )
    with pytest.raises(ValueError, match="handler must be a simple Python module name"):
        load_tools(config)
