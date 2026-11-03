import json
from pathlib import Path

import pytest

from harness.subagents import SubagentManager, load_profiles
from harness.tools.access import make_profile_tool_access_persister
from harness.tools.artifacts import ArtifactTool
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import load_tools


@pytest.fixture
def catalog(tmp_path):
    catalog = ToolCatalog(
        load_tools(Path(__file__).parents[1] / "tools.json"), tmp_path / "artifacts"
    )
    for name, content in (
        (
            "helper",
            json.dumps(
                {"description": "Helper", "input_schema": {"type": "object"}, "command": ["true"]}
            ),
        ),
        ("broken", "{"),
    ):
        directory = catalog.artifact_root / name
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(content)
    return catalog


def write_profile(tmp_path, **fields):
    profile = {
        "description": "Test",
        "system_prompt": "Test",
        "permission_mode": "inherit",
        "tools": [],
        **fields,
    }
    path = tmp_path / "subagents.json"
    path.write_text(json.dumps({"profiles": {"test": profile}}))
    return path


@pytest.mark.parametrize("as_mapping", [False, True])
def test_load_merges_fields_without_loading_unassigned_artifacts(tmp_path, catalog, as_mapping):
    path = write_profile(tmp_path, tools=["bash"], tool_artifacts=["helper"])
    available = catalog if as_mapping else [catalog["bash"], catalog["helper"]]
    profile = load_profiles(path, available)["test"]
    assert profile.tool_artifacts == ("helper",)
    if as_mapping:
        assert not any(isinstance(tool, ArtifactTool) for tool in profile.tools)
    else:
        assert [tool.definition.name for tool in profile.tools][:2] == ["bash", "helper"]


def test_optional_artifacts_default_does_not_change_file(tmp_path, catalog):
    path = write_profile(tmp_path, tools=["bash"])
    original = path.read_bytes()
    assert load_profiles(path, catalog)["test"].tools[0] is catalog["bash"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("field", ["tools", "tool_artifacts"])
@pytest.mark.parametrize("value", [None, "bash", {}, [1], [True], [None]])
def test_tool_fields_require_string_arrays(tmp_path, catalog, field, value):
    path = write_profile(tmp_path, **{field: value})
    with pytest.raises(ValueError, match=f"{field} must be an array of strings"):
        load_profiles(path, catalog)


def test_native_tools_field_is_required(tmp_path, catalog):
    path = write_profile(tmp_path)
    raw = json.loads(path.read_text())
    del raw["profiles"]["test"]["tools"]
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="tools must be an array of strings"):
        load_profiles(path, catalog)


@pytest.mark.parametrize(
    "fields,match",
    [
        ({"tools": ["bash", "bash"]}, "duplicate tool assignment 'bash'"),
        ({"tool_artifacts": ["helper", "helper"]}, "duplicate tool assignment 'helper'"),
        ({"tools": ["helper"], "tool_artifacts": ["helper"]}, "duplicate tool assignment"),
        ({"tools": ["helper"]}, "unknown tool 'helper'"),
        ({"tool_artifacts": ["bash"]}, "belongs in tools, not tool_artifacts"),
        ({"tools": ["missing"]}, "unknown tool 'missing'"),
        ({"tools": ["broken"]}, "unknown tool 'broken'"),
    ],
)
def test_invalid_tool_assignments_fail_without_migration(tmp_path, catalog, fields, match):
    path = write_profile(tmp_path, **fields)
    original = path.read_bytes()
    with pytest.raises(ValueError, match=match):
        load_profiles(path, catalog)
    assert path.read_bytes() == original


def test_invalid_assigned_artifact_is_retained_but_not_exposed(tmp_path, catalog):
    path = write_profile(tmp_path, tools=["bash"], tool_artifacts=["broken", "helper"])
    original = path.read_bytes()
    profile = load_profiles(path, catalog)["test"]
    assert profile.tool_artifacts == ("broken", "helper")
    assert profile.unavailable_artifacts == {}
    manager = SubagentManager(lambda _: None, {"test": profile})
    profile = manager._resolve_tools(profile)
    assert "broken" in profile.unavailable_artifacts
    assert str(catalog.artifact_root / "broken") in profile.unavailable_artifacts["broken"]
    assert {tool.definition.name for tool in profile.tools} >= {"bash", "helper"}
    assert "broken" not in {tool.definition.name for tool in profile.tools}
    assert path.read_bytes() == original


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O failure")])
def test_artifact_read_failure_is_deferred_until_resolution(tmp_path, catalog, monkeypatch, error):
    path = write_profile(tmp_path, tool_artifacts=["helper"])
    original = path.read_bytes()
    read_text = Path.read_text
    manifest = catalog.artifact_root / "helper" / "manifest.json"

    def fail_manifest_read(self, *args, **kwargs):
        if self == manifest:
            raise error
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_manifest_read)
    profile = load_profiles(path, catalog)["test"]
    assert not profile.unavailable_artifacts
    manager = SubagentManager(lambda _: None, {"test": profile})
    resolved = manager._resolve_tools(profile)
    assert str(error) in resolved.unavailable_artifacts["helper"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("name,field", [("bash", "tools"), ("helper", "tool_artifacts")])
@pytest.mark.parametrize("existing", [False, True])
async def test_save_resolves_only_requested_tool_and_syncs(
    tmp_path, catalog, name, field, existing
):
    path = write_profile(tmp_path, **({field: [name]} if existing else {}))
    approvals, synced = [], []

    async def approve(_definition, arguments):
        approvals.append(arguments)
        return True

    persist = make_profile_tool_access_persister(
        path, "test", approve, catalog, on_saved=synced.append
    )
    assert await persist(name)
    assert synced == [name]
    assert len(approvals) == (0 if existing else 1)
    stored = json.loads(path.read_text())["profiles"]["test"]
    assert stored[field] == [name]
    assert name not in stored.get("tools" if field == "tool_artifacts" else "tool_artifacts", [])
    if name == "bash":
        assert ("tool_artifacts" in stored) == (not existing)


@pytest.mark.parametrize("during_approval", [False, True])
@pytest.mark.parametrize(
    "fields,name",
    [
        ({"tools": ["helper"]}, "helper"),
        ({"tool_artifacts": ["bash"]}, "bash"),
        ({"tool_artifacts": None}, "helper"),
        ({"tool_artifacts": [1]}, "helper"),
        ({"tools": ["bash", "bash"]}, "helper"),
        ({"tools": ["helper"], "tool_artifacts": ["helper"]}, "helper"),
    ],
)
async def test_invalid_save_never_syncs_or_repairs_edits(
    tmp_path, catalog, fields, name, during_approval
):
    path = write_profile(tmp_path, **({} if during_approval else fields))
    errors, synced, approvals = [], [], []

    async def approve(_definition, arguments):
        approvals.append(arguments)
        write_profile(tmp_path, **fields)
        return True

    persist = make_profile_tool_access_persister(
        path, "test", approve, catalog, report_error=errors.append, on_saved=synced.append
    )
    assert not await persist(name)
    assert synced == []
    assert len(approvals) == int(during_approval)
    assert errors
    stored = json.loads(path.read_text())["profiles"]["test"]
    for key, value in fields.items():
        assert stored[key] == value


@pytest.mark.parametrize("name", ["missing", "broken"])
async def test_unresolvable_save_does_not_approve_or_sync(tmp_path, catalog, name):
    path = write_profile(tmp_path)
    original = path.read_bytes()
    synced = []

    async def approve(*_args):
        pytest.fail("Unresolvable tools cannot be saved")

    persist = make_profile_tool_access_persister(
        path, "test", approve, catalog, on_saved=synced.append
    )
    assert not await persist(name)
    assert synced == []
    assert path.read_bytes() == original
