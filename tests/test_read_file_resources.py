from dataclasses import replace
from pathlib import Path

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall, ToolResult
from harness.permissions import AlwaysAllow
from harness.provider import MockProvider
from harness.skills import Skill, SkillCatalog, load_skills
from harness.tools.base import ToolContext, resolve_read_path
from harness.tools.handlers.read_file import execute
from harness.tools.loader import load_tools
from harness.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def context(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = {}
    for name in ("first", "second"):
        root = tmp_path / "arbitrary" / name
        (root / "nested").mkdir(parents=True)
        manifest = root / "SKILL.md"
        manifest.write_text("Full workflow instructions")
        skills[name] = Skill(name, "Test workflow", root, manifest)
    return ToolContext(
        workspace,
        allowed_read_roots=tuple(skill.root for skill in skills.values()),
        skill_catalog=SkillCatalog(skills),
        allowed_skills=tuple(skills),
    )


def resource(context, name="first", path="nested/resource.txt", text="skill data"):
    target = context.skill_catalog.get(name).root / path
    target.write_text(text)
    return target


async def test_workspace_precedence_and_explicit_override(context):
    first = resource(context)
    resource(context, "second", text="other skill")
    workspace = context.working_directory / "nested"
    workspace.mkdir()
    (workspace / "resource.txt").write_text("workspace data")
    assert resolve_read_path(context, "nested/resource.txt") == workspace / "resource.txt"
    result = await execute(context, {"path": "nested/resource.txt"})
    assert not result.is_error and result.content == "workspace data"
    result = await execute(context, {"path": "nested/resource.txt", "skill": "first"})
    assert not result.is_error and result.content == first.read_text()


async def test_unique_arbitrary_layout_and_deduplication(context):
    target = resource(context)
    alias = target.parent.parent.parent / "alias"
    alias.symlink_to(target.parent.parent, target_is_directory=True)
    context = replace(context, allowed_read_roots=(*context.allowed_read_roots, alias))
    assert resolve_read_path(context, "nested/resource.txt") == target
    result = await execute(context, {"path": "nested/resource.txt"})
    assert not result.is_error and result.content == "skill data"


async def test_ambiguity_lists_safe_candidates(context):
    targets = [resource(context, name) for name in ("first", "second")]
    result = await execute(context, {"path": "nested/resource.txt"})
    assert result.is_error and "ambiguous" in result.content
    for target in targets:
        assert str(target) in result.content


@pytest.mark.parametrize("skill", [None, False, 3, [], {}, "", "unassigned", "unknown"])
async def test_invalid_or_unassigned_selector(context, skill):
    resource(context)
    context = replace(context, allowed_skills=("first", "unknown"))
    result = await execute(context, {"path": "nested/resource.txt", "skill": skill})
    assert result.is_error
    if skill == "unknown":
        assert "unknown skill" in result.content


async def test_catalog_assignment_does_not_expand_read_grants(context):
    target = resource(context)
    context = replace(context, allowed_read_roots=())
    result = await execute(context, {"path": "nested/resource.txt", "skill": "first"})
    assert result.is_error and "allowed skill" in result.content
    result = await execute(context, {"path": str(target)})
    assert result.is_error
    # Explicit selection can use an existing workspace grant even without extra roots.
    context = replace(context, working_directory=target.parent.parent)
    result = await execute(context, {"path": "nested/resource.txt", "skill": "first"})
    assert not result.is_error


async def test_explicit_missing_has_no_workspace_or_other_skill_fallback(context):
    resource(context, "second")
    (context.working_directory / "workspace.txt").write_text("workspace")
    for path in ("nested/resource.txt", "workspace.txt"):
        result = await execute(context, {"path": path, "skill": "first"})
        assert result.is_error and "No such file" in result.content


async def test_absolute_paths_and_explicit_confinement(context, tmp_path):
    target = resource(context)
    result = await execute(context, {"path": str(target)})
    assert not result.is_error
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    link = target.parent / "escape.txt"
    link.symlink_to(outside)
    for path in (str(target), "../second/SKILL.md", "nested/escape.txt"):
        result = await execute(context, {"path": path, "skill": "first"})
        assert result.is_error
    for path in (str(outside), "../outside.txt", "nested/escape.txt"):
        result = await execute(context, {"path": path})
        assert result.is_error


@pytest.mark.parametrize("path", [None, False, 1, [], {}])
async def test_invalid_path_types(context, path):
    result = await execute(context, {"path": path})
    assert result.is_error and "path must be a string" in result.content


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid_utf8", "dangling", "ancestor"])
async def test_workspace_errors_are_not_masked(context, kind):
    resource(context)
    workspace = context.working_directory / "nested"
    if kind == "ancestor":
        workspace.symlink_to(context.working_directory / "absent", target_is_directory=True)
    else:
        workspace.mkdir()
        path = workspace / "resource.txt"
        if kind == "directory":
            path.mkdir()
        elif kind == "invalid_utf8":
            path.write_bytes(b"\xff")
        elif kind == "dangling":
            path.symlink_to(workspace / "absent")
    requested = "missing.txt" if kind == "missing" else "nested/resource.txt"
    result = await execute(context, {"path": requested})
    assert result.is_error
    expected = {"directory": "Is a directory", "invalid_utf8": "decode"}
    assert expected.get(kind, "No such file") in result.content


@pytest.mark.parametrize("error", [PermissionError("denied"), NotADirectoryError("not directory")])
async def test_workspace_stat_errors_do_not_fall_back(context, monkeypatch, error):
    resource(context)
    original = Path.stat
    workspace_path = context.working_directory / "nested/resource.txt"

    def stat(path, *args, **kwargs):
        if path == workspace_path:
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    result = await execute(context, {"path": "nested/resource.txt"})
    assert result.is_error and str(error) in result.content


async def test_project_files_are_not_filtered_by_skill_assignment(context):
    path = context.working_directory / "unassigned-skill.txt"
    path.write_text("project data")
    result = await execute(replace(context, allowed_skills=()), {"path": path.name})
    assert not result.is_error and result.content == "project data"


async def test_disallowed_workspace_symlink_does_not_fall_back(context, tmp_path):
    resource(context)
    (context.working_directory / "nested").symlink_to(tmp_path / "ungranted")
    result = await execute(context, {"path": "nested/resource.txt"})
    assert result.is_error and "allowed skill" in result.content


async def test_agent_activation_then_bare_real_lab_resource(tmp_path):
    catalog = load_skills(ROOT / "skills.json", ROOT, tmp_path / "isolated-user")
    skill = catalog.get("lab-resource-check")
    provider = MockProvider(
        [
            ProviderResponse(
                [ToolCall("activate", "activate_skill", {"name": skill.name})], "tool_use"
            ),
            ProviderResponse(
                [ToolCall("fixture", "read_file", {"path": "data/check.json"})], "tool_use"
            ),
            ProviderResponse([TextBlock("Done")], "end_turn"),
        ]
    )
    agent = Agent(
        provider,
        ToolRegistry(load_tools(ROOT / "tools.json")),
        AlwaysAllow(),
        ToolContext(
            tmp_path,
            allowed_read_roots=(skill.root,),
            skill_catalog=catalog.for_names((skill.name,)),
            allowed_skills=(skill.name,),
        ),
    )
    await agent.send("Use the resource check skill")
    results = [
        block
        for message in agent.messages
        for block in message.content
        if isinstance(block, ToolResult) and block.tool_use_id == "fixture"
    ]
    assert len(results) == 1 and not results[0].is_error
    assert results[0].content == (skill.root / "data/check.json").read_text()
    assert "SKILL_LAB_RESOURCE_V1" in results[0].content
    protected = [message for message in agent.messages if message.preserve_from_compaction]
    assert len(protected) == 1
    assert skill.manifest.read_text().rstrip() in protected[0].content[0].text
    assert "prefers existing workspace paths" in protected[0].content[0].text
