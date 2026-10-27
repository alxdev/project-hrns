import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from harness.api.types import ToolDefinition
from harness.subagents import SubagentManager, SubagentProfile, load_profiles
from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.artifacts import ArtifactTool
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.registry import ToolRegistry


def manifest(root, description="Helper", schema=None):
    directory = root / "helper"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "description": description,
                "input_schema": schema if schema is not None else {"type": "object"},
                "command": ["never-execute"],
            }
        )
    )
    return path


@pytest.fixture
def loads(monkeypatch):
    import harness.tools.catalog as module

    spy = Mock(wraps=module.load_artifact_tool)
    monkeypatch.setattr(module, "load_artifact_tool", spy)
    return spy


def test_cache_reads_and_validates_once_and_ignores_unrelated_files(tmp_path, loads):
    path = manifest(tmp_path)
    catalog = ToolCatalog([], tmp_path)
    first = catalog["helper"]
    (path.parent / "payload.txt").write_text("changed")
    (tmp_path / "another").mkdir()
    assert catalog["helper"] is first
    assert loads.call_count == 1
    manifest(tmp_path, "Replacement")
    assert catalog["helper"].definition.description == "Replacement"
    assert catalog["helper"] is not first
    assert loads.call_count == 2


def test_invalid_manifest_cache_is_revalidated_after_repair_and_removal(tmp_path, loads):
    path = manifest(tmp_path, schema={"type": "object", "required": "bad"})
    catalog = ToolCatalog([], tmp_path)
    for _ in range(2):
        with pytest.raises(InvalidArtifactError, match="invalid input_schema"):
            catalog["helper"]
    assert loads.call_count == 1
    manifest(tmp_path, "Repaired")
    assert catalog["helper"].definition.description == "Repaired"
    assert loads.call_count == 2
    path.unlink()
    for _ in range(2):
        with pytest.raises(InvalidArtifactError, match="missing manifest.json"):
            catalog["helper"]
    assert loads.call_count == 3
    manifest(tmp_path, "Restored")
    assert catalog["helper"].definition.description == "Restored"
    assert loads.call_count == 4


def test_caches_are_isolated_by_artifact_root(tmp_path, loads):
    left, right = tmp_path / "left", tmp_path / "right"
    manifest(left, "Left")
    manifest(right, "Right")
    assert ToolCatalog([], left)["helper"].definition.description == "Left"
    assert ToolCatalog([], right)["helper"].definition.description == "Right"
    assert loads.call_count == 2


@pytest.mark.parametrize(
    "change,status,diagnostic",
    [
        ("invalid", "existing_artifact_invalid", "invalid manifest JSON"),
        ("missing_manifest", "existing_artifact_invalid", "missing manifest.json"),
        ("missing_directory", "not_installed", "missing artifact directory"),
    ],
)
async def test_request_tool_revokes_artifact_discovered_invalid_or_missing(
    tmp_path, change, status, diagnostic
):
    from harness.tools.handlers.request_tool import execute

    path = manifest(tmp_path)
    catalog = ToolCatalog([], tmp_path)
    registry = ToolRegistry([catalog["helper"]])
    context = ToolContext(tmp_path, tool_catalog=catalog, tool_registry=registry)
    arguments = {"name": "helper", "reason": "Need helper"}
    first = await execute(context, arguments)
    assert json.loads(first.content)["status"] == "already_available"
    if change == "invalid":
        path.write_text("{")
    elif change == "missing_manifest":
        path.unlink()
    else:
        path.parent.rename(tmp_path / "removed_helper")
    result = await execute(context, arguments)
    payload = json.loads(result.content)
    assert result.is_error
    assert payload["status"] == status
    assert diagnostic in payload["message"]
    assert registry.get("helper") is None
    assert registry.definitions() == []


async def test_request_tool_preserves_valid_definition_within_active_delegation(tmp_path):
    from harness.tools.handlers.request_tool import execute

    manifest(tmp_path, "Original")
    catalog = ToolCatalog([], tmp_path)
    original = catalog["helper"]
    registry = ToolRegistry([original])
    manifest(tmp_path, "Updated")
    context = ToolContext(tmp_path, tool_catalog=catalog, tool_registry=registry)
    result = await execute(context, {"name": "helper", "reason": "Need helper"})
    assert not result.is_error
    assert json.loads(result.content)["status"] == "already_available"
    assert registry.get("helper") is original
    assert catalog["helper"].definition.description == "Updated"


def test_native_schema_validation_is_lazy_cached_and_tracks_definition_changes(
    tmp_path, monkeypatch
):
    import harness.tools.catalog as module
    from harness.tools.loader import ConfiguredTool

    tool = ConfiguredTool(ToolDefinition("native", "Native", {"type": "object"}), Mock())
    catalog = ToolCatalog([tool], tmp_path)
    validator = Mock(wraps=module.validator_for)
    monkeypatch.setattr(module, "validator_for", validator)
    assert catalog.native_tools["native"] is tool
    assert validator.call_count == 0
    assert catalog["native"] is tool
    assert catalog["native"] is tool
    assert validator.call_count == 1
    tool.definition.input_schema["required"] = "invalid"
    for _ in range(2):
        with pytest.raises(ValueError, match="invalid input_schema for tool 'native'"):
            catalog["native"]
    assert validator.call_count == 2
    tool.definition.input_schema["required"] = ["text"]
    assert catalog["native"] is tool
    assert validator.call_count == 3


def test_startup_reads_only_config_without_artifact_stat_or_scan(tmp_path, monkeypatch, loads):
    root = tmp_path / "artifacts"
    manifest(root)
    catalog = ToolCatalog([], root)
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "profiles": {
                    "lab": {
                        "description": "Test",
                        "system_prompt": "Test",
                        "permission_mode": "inherit",
                        "tools": [],
                        "tool_artifacts": ["helper", "missing"],
                    }
                }
            }
        )
    )
    original = Path.lstat

    def no_artifact_stat(self, *args, **kwargs):
        if self == root or root in self.parents:
            pytest.fail("startup statted an artifact")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", no_artifact_stat)
    monkeypatch.setattr(Path, "iterdir", lambda _: pytest.fail("startup scanned artifacts"))
    profiles = load_profiles(path, catalog)
    manager = SubagentManager(lambda _: pytest.fail("startup created an agent"), profiles)
    manager.describe()
    assert profiles["lab"].tool_artifacts == ("helper", "missing")
    assert profiles["lab"].unavailable_artifacts == {}
    assert loads.call_count == 0


class RecordingAgent:
    def __init__(self, profile):
        self.tools = ToolRegistry(profile.tools)
        self.calls = []

    async def send(self, prompt):
        self.calls.append((prompt, self.tools.definitions()))
        return "done"


@pytest.mark.parametrize("assigned", [False, True])
@pytest.mark.parametrize("change", ["invalid", "missing_manifest", "missing_directory"])
async def test_request_revocation_is_not_undone_by_next_delegation(tmp_path, assigned, change):
    from harness.tools.handlers.request_tool import execute

    path = manifest(tmp_path)
    catalog = ToolCatalog([], tmp_path)
    profile = SubagentProfile(
        "lab",
        "Test",
        "Test",
        reuse=True,
        tool_catalog=catalog,
        tool_artifacts=("helper",) if assigned else (),
    )
    manager = SubagentManager(RecordingAgent, {"lab": profile})
    await manager.delegate("lab", "Initialize")
    agent = manager._agents["lab"]
    if not assigned:
        agent.tools.register(catalog["helper"])
        await manager.delegate("lab", "Remember session grant")
        assert "helper" in manager._session_artifacts["lab"]
    if change == "invalid":
        path.write_text("{")
    elif change == "missing_manifest":
        path.unlink()
    else:
        path.parent.rename(tmp_path / "removed_helper")
    context = ToolContext(tmp_path, tool_catalog=catalog, tool_registry=agent.tools)
    result = await execute(context, {"name": "helper", "reason": "Need helper"})
    assert result.is_error
    assert agent.tools.get("helper") is None
    await manager.delegate("lab", "After revocation")
    assert manager._agents["lab"] is agent
    assert agent.tools.get("helper") is None
    assert agent.calls[-1][1] == []
    assert manager.profiles["lab"].tools == ()
    assert manager.profiles["lab"].tool_artifacts == (("helper",) if assigned else ())


async def test_session_grant_survives_invalid_manifest_without_becoming_persisted(tmp_path):
    path = manifest(tmp_path)
    catalog = ToolCatalog([], tmp_path)
    profile = SubagentProfile("lab", "Test", "Test", reuse=True, tool_catalog=catalog)
    manager = SubagentManager(RecordingAgent, {"lab": profile})
    await manager.delegate("lab", "Initialize")
    agent = manager._agents["lab"]
    agent.tools.register(catalog["helper"])
    path.write_text("{")
    await manager.delegate("lab", "Detect invalid session grant")
    assert agent.tools.get("helper") is None
    assert "invalid manifest JSON" in agent.calls[-1][0]
    manifest(tmp_path, "Replacement")
    manager.refresh_artifact_access(catalog["helper"])
    assert agent.tools.get("helper").definition.description == "Replacement"
    await manager.delegate("lab", "Retain session grant")
    assert agent.tools.get("helper") is catalog["helper"]
    assert manager.profiles["lab"].tool_artifacts == ()
    assert manager.profiles["lab"].tools == ()


async def test_delegations_share_cache_and_refresh_reused_registry(tmp_path, loads):
    path = manifest(tmp_path)
    catalog = ToolCatalog([], tmp_path)
    profile = SubagentProfile(
        "lab", "Test", "Test", reuse=True, tool_artifacts=("helper",), tool_catalog=catalog
    )
    created, warnings = [], []

    def factory(profile):
        agent = RecordingAgent(profile)
        created.append(agent)
        return agent

    manager = SubagentManager(
        factory,
        {"lab": profile, "other": replace(profile, name="other")},
        report_unavailable=warnings.append,
    )
    await manager.delegate("lab", "First")
    first = created[0].tools.get("helper")
    await manager.delegate("lab", "Second")
    await manager.delegate("other", "Shared")
    assert loads.call_count == 1
    manifest(tmp_path, "Updated")
    await manager.delegate("lab", "Refresh")
    assert created[0].tools.get("helper") is not first
    assert created[0].tools.get("helper").definition.description == "Updated"
    assert loads.call_count == 2
    path.write_text("{")
    await manager.delegate("lab", "Malformed")
    await manager.delegate("lab", "Still malformed")
    assert loads.call_count == 3
    assert created[0].tools.get("helper") is None
    assert "invalid manifest JSON" in warnings[-1]
    assert "invalid manifest JSON" in created[0].calls[-1][0]
    assert manager.profiles["lab"].tool_artifacts == ("helper",)
    assert len(created) == 2
    manifest(tmp_path, "Repaired")
    manager.refresh_artifact_access(catalog["helper"])
    assert created[0].tools.get("helper").definition.description == "Repaired"
    await manager.delegate("lab", "After explicit replacement")
    assert loads.call_count == 4
    assert not manager.profiles["lab"].unavailable_artifacts


@pytest.mark.parametrize(
    "schema,arguments,diagnostic",
    [
        ({"type": "object", "required": ["text"]}, {}, "Invalid arguments"),
        ({"type": "object", "$ref": "https://example.invalid/schema"}, {}, "locally"),
        (
            {
                "type": "object",
                "$defs": {"text": {"type": "string"}},
                "properties": {"text": {"$ref": "#/$defs/text"}},
            },
            {"text": 1},
            "Invalid arguments",
        ),
    ],
)
async def test_argument_errors_do_not_execute_or_fetch(
    tmp_path, monkeypatch, schema, arguments, diagnostic
):
    import socket

    import harness.tools.artifacts as module

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(module, "run_process", lambda *_args, **_kwargs: pytest.fail("executed"))
    tool = ArtifactTool(ToolDefinition("helper", "Helper", schema), tmp_path, ("never",))
    result = await tool.execute(ToolContext(tmp_path), arguments)
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["status"] == "invalid_arguments"
    assert diagnostic in payload["message"]


@pytest.mark.parametrize("case", ["value", "schema", "property", "reference"])
async def test_argument_diagnostics_are_bounded_and_redact_values_and_keys(tmp_path, case):
    secret = "SECRET_SENTINEL_" + "x" * 10000
    if case == "value":
        schema = {"type": "object", "properties": {secret: {"type": "integer"}}}
        arguments = {secret: secret}
        keyword = "type"
    elif case == "schema":
        schema = {"type": "object", "properties": {"text": {"enum": [secret]}}}
        arguments = {"text": "other"}
        keyword = "enum"
    elif case == "property":
        schema = {"type": "object", "additionalProperties": False}
        arguments = {secret: "other"}
        keyword = "additionalProperties"
    else:
        schema = {"type": "object", "$ref": f"https://example.invalid/{secret}"}
        arguments = {}
        keyword = "locally"
    tool = ArtifactTool(ToolDefinition("helper", "Helper", schema), tmp_path, ("never",))
    result = await tool.execute(ToolContext(tmp_path), arguments)
    assert result.is_error
    assert "SECRET_SENTINEL" not in result.content
    assert len(result.content) < 700
    assert keyword in result.content
