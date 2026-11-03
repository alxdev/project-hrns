import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock
from harness.permissions import AlwaysAllow
from harness.provider import MockProvider
from harness.skills import Skill, SkillCatalog
from harness.subagents import DelegationTool, SubagentManager, load_profiles
from harness.subagents.capabilities import Capability, CapabilitySummary, format_capabilities
from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import load_tools
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


def setup_manager(tmp_path, **options):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    path = tmp_path / "subagents.json"
    path.write_text(
        json.dumps(
            {
                "profiles": {
                    "analyst": {
                        "description": "Analyze documents.",
                        "system_prompt": "PRIVATE ROLE INSTRUCTIONS",
                        "tools": ["read_file"],
                        "tool_artifacts": ["reporter"],
                        "skills": ["review"],
                        "permission_mode": "inherit",
                    }
                }
            }
        )
    )
    skills = SkillCatalog(
        {
            "review": Skill("review", "Review documents.", tmp_path, tmp_path / "PRIVATE_SKILL.md"),
            "other": Skill("other", "Unassigned skill", tmp_path, tmp_path / "other.md"),
        }
    )
    manager = SubagentManager(
        lambda _profile: None, load_profiles(path, catalog), skill_catalog=skills, **options
    )
    tool = DelegationTool("analyst", "Original description")
    tool.bind(manager)
    return catalog, manager, tool


def test_summary_exposes_metadata_without_reading_or_validating_resources(tmp_path, monkeypatch):
    catalog, manager, tool = setup_manager(tmp_path)

    def forbidden(*_args):
        pytest.fail("Summary must not discover or validate artifacts or activate skills")

    monkeypatch.setattr(ToolCatalog, "__getitem__", forbidden)
    monkeypatch.setattr(ToolCatalog, "__iter__", forbidden)
    monkeypatch.setattr(catalog, "_signature", forbidden)
    monkeypatch.setattr(SkillCatalog, "activate", forbidden)
    description = tool.definition.description

    assert description.startswith("Analyze documents.")
    assert "native tool read_file [assigned; configured] — Read a UTF-8 text file" in description
    assert "native tool bash [requestable; configured]" in description
    assert "artifact reporter [assigned; undiscovered]" in description
    assert "skill review [assigned; cataloged] — Review documents." in description
    for control in ("list_available_tools", "request_tool", "activate_skill"):
        assert f"control {control} [assigned; configured]" in description
    assert "Permission mode: inherit" in description
    assert "do not guarantee approval or execution" in description
    assert "PRIVATE" not in description and "Unassigned skill" not in description
    assert "input_schema" not in description and "properties" not in description
    assert description.replace("\n", "\n    ") in manager.describe()
    assert tool.definition.name == "delegate_analyst"
    assert tool.definition.input_schema["required"] == ["task"]
    assert set(tool.definition.input_schema["properties"]) == {"task"}


def test_artifact_observations_refresh_without_summary_filesystem_access(tmp_path, monkeypatch):
    catalog, manager, tool = setup_manager(tmp_path)
    assert "reporter [assigned; undiscovered]" in tool.definition.description
    with pytest.raises(KeyError):
        catalog["reporter"]
    assert "reporter [assigned; unavailable (missing)]" in tool.definition.description

    directory = catalog.artifact_root / "reporter"
    directory.mkdir(parents=True)
    manifest = directory / "manifest.json"
    manifest.write_text("{}")
    with pytest.raises(InvalidArtifactError):
        catalog["reporter"]
    assert "reporter [assigned; unavailable (invalid)]" in tool.definition.description
    manifest.write_text(
        json.dumps(
            {
                "description": "Summarize findings",
                "input_schema": {"type": "object"},
                "command": ["python3", "run.py"],
            }
        )
    )
    repaired = catalog["reporter"]
    assert "artifact reporter [assigned; last validated] — Summarize findings" in (
        tool.definition.description
    )
    manager.refresh_artifact_access(repaired)
    # A cached invalid lookup takes precedence over an old assigned tool object.
    manifest.write_text("{}")
    with pytest.raises(InvalidArtifactError):
        catalog["reporter"]
    assert "reporter [assigned; unavailable (invalid)]" in tool.definition.description

    def forbidden(*_args):
        pytest.fail("Summary must not revalidate cached observations")

    monkeypatch.setattr(catalog, "_signature", forbidden)
    assert "unavailable (invalid)" in tool.definition.description


async def test_live_session_grants_do_not_become_future_capabilities(tmp_path):
    catalog, manager, delegation = setup_manager(tmp_path)
    reached = asyncio.Event()
    resume = asyncio.Event()

    async def respond(_messages, _definitions):
        child.tools.register(catalog["bash"])
        reached.set()
        await resume.wait()
        return ProviderResponse([TextBlock("done")], "end_turn")

    child = Agent(
        MockProvider(responder=respond),
        ToolRegistry(),
        AlwaysAllow(),
        ToolContext(tmp_path),
    )
    manager.factory = lambda _profile: child
    task = asyncio.create_task(manager.delegate("analyst", "Analyze"))
    await asyncio.wait_for(reached.wait(), 2)
    try:
        assert "bash [requestable; configured]" in delegation.definition.description
        assert "bash [assigned; configured]" not in delegation.definition.description
        manager.grant_tool_access("analyst", catalog["bash"])
        assert "bash [assigned; configured]" in delegation.definition.description
        assert "bash [requestable; configured]" not in delegation.definition.description
    finally:
        resume.set()
        await task


def test_missing_skill_metadata_and_absent_request_control_are_explicit(tmp_path):
    _catalog, manager, delegation = setup_manager(tmp_path)
    profile = manager.profiles["analyst"]
    manager.profiles["analyst"] = replace(profile, tools=(), skills=("missing",))
    description = delegation.definition.description
    assert "skill missing [assigned; metadata unavailable]" in description
    assert "native tool bash [unassigned; configured]" in description
    assert "[requestable;" not in description


async def test_removed_profile_preserves_delegation_error_result(tmp_path):
    _catalog, manager, delegation = setup_manager(tmp_path)
    manager.profiles.clear()
    result = await delegation.execute(ToolContext(tmp_path), {"task": "Analyze"})
    assert result.is_error
    assert "unknown subagent" in result.content
    assert delegation.definition.name == "delegate_analyst"


@pytest.mark.parametrize("failure", ["signature", "load"])
def test_new_artifact_io_failure_replaces_cached_valid_observation(tmp_path, monkeypatch, failure):
    catalog, manager, delegation = setup_manager(tmp_path)
    directory = catalog.artifact_root / "reporter"
    directory.mkdir(parents=True)
    manifest = directory / "manifest.json"
    content = json.dumps(
        {
            "description": "Summarize findings",
            "input_schema": {"type": "object"},
            "command": ["python3", "run.py"],
        }
    )
    manifest.write_text(content)
    manager._resolve_tools(manager.profiles["analyst"])
    assert "reporter [assigned; last validated]" in delegation.definition.description
    manifest.write_text(content + "\n")

    def fail(*_args):
        raise PermissionError("Artifact access failed")

    with monkeypatch.context() as patch:
        if failure == "signature":
            patch.setattr(catalog, "_signature", fail)
        else:
            patch.setattr("harness.tools.catalog.load_artifact_tool", fail)
        manager._resolve_tools(manager.profiles["analyst"])
        assert "reporter" in manager.profiles["analyst"].unavailable_artifacts
        assert "reporter [assigned; unavailable (load failed)]" in (
            delegation.definition.description
        )
        assert "Artifact access failed" in delegation.definition.description
        assert "reporter [assigned; last validated]" not in delegation.definition.description

    # Transient errors are observed but do not suppress a later normal retry.
    manager._resolve_tools(manager.profiles["analyst"])
    assert "reporter [assigned; last validated]" in delegation.definition.description


def test_large_inventory_is_explicitly_truncated_without_hiding_assigned_skills():
    resources = tuple(
        Capability(str(index), kind, access, "known", "long " * 100)
        for kind, access in (("MCP tool", "assigned"), ("native tool", "requestable"))
        for index in range(20)
    ) + tuple(Capability(f"skill{index}", "skill", "assigned", "cataloged") for index in range(20))
    text = format_capabilities(CapabilitySummary("Role", "deny", resources))
    assert "Additional discovered MCP tools: 8 (omitted)." in text
    assert "Additional unassigned resources: 8 (omitted)." in text
    assert "skill skill19 [assigned; cataloged]" in text
    assert "long " * 100 not in text
