"""Recovery checks use temporary artifacts only; no authoring executes commands."""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from harness.tools.access import make_profile_tool_access_persister
from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.artifact_recovery import OwnedStaging, rename_exclusive
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.handlers import tool_authoring
from harness.tools.loader import load_artifact_tool, load_tools
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


def symlink(path, target):
    try:
        path.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")


def proposal(**overrides):
    return {
        "name": "helper",
        "description": "Replacement",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "command": ["runtime", "{artifact_dir}/main.py"],
        "files": {"main.py": "raise AssertionError('must not execute')"},
        "replace_invalid": True,
        **overrides,
    }


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "artifacts"
    old = root / "helper"
    old.mkdir(parents=True)
    (old / "manifest.json").write_text("{")
    (old / "original.txt").write_text("preserve me")
    catalog = ToolCatalog(load_tools(TOOLS), root)
    approval = AsyncMock(return_value=True)
    changed = Mock()
    context = ToolContext(
        tmp_path,
        artifact_root=root,
        tool_catalog=catalog,
        tool_registry=ToolRegistry(),
        approve_artifact_replacement=approval,
        on_artifact_replaced=changed,
    )
    return context, old, approval, changed


async def run(context, **overrides):
    result = await tool_authoring.execute(context, proposal(**overrides))
    return result, json.loads(result.content)


def unchanged(old):
    assert (old / "manifest.json").read_text() == "{"
    assert (old / "original.txt").read_text() == "preserve me"
    assert not list(old.parent.glob(".harness-stage-*"))


async def test_recovery_approves_before_staging_and_preserves_original(setup, monkeypatch):
    context, old, approval, changed = setup

    def forbidden(*args, **kwargs):
        pytest.fail("authoring must never execute a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr("harness.tools.artifacts.run_process", forbidden)

    async def approve(definition, details):
        assert definition.name == "recreate_invalid_artifact"
        assert details == {
            "name": "helper",
            "path": str(old),
            "archive_path": str(old.parent / "INVALID_helper"),
            "reason": "invalid manifest JSON: Expecting property name enclosed in double quotes",
            "replacement": {
                k: v for k, v in proposal().items() if k not in ("name", "replace_invalid")
            },
        }
        assert not list(old.parent.glob(".harness-stage-*"))
        assert (old / "manifest.json").read_text() == "{"
        return True

    approval.side_effect = approve
    result, payload = await run(context)
    assert not result.is_error and payload["status"] == "recreated"
    assert payload["execution_verified"] is False
    archive = Path(payload["archive_path"])
    assert (archive / "manifest.json").read_text() == "{"
    assert (archive / "original.txt").read_text() == "preserve me"
    assert context.tool_registry.get("helper").definition.description == "Replacement"
    assert context.tool_registry.get("helper").directory == old
    changed.assert_called_once()
    assert "INVALID_helper" not in context.tool_catalog
    assert "INVALID_helper" not in list(context.tool_catalog)
    with pytest.raises(ValueError):
        load_artifact_tool(old.parent, "INVALID_helper")
    assert not list(old.parent.glob(".harness-stage-*"))


@pytest.mark.parametrize("approval_kind", ["deny", "absent"])
async def test_separate_approval_required(setup, approval_kind):
    context, old, approval, changed = setup
    approval.return_value = False
    if approval_kind == "absent":
        context = replace(context, approve_artifact_replacement=None)
    result, payload = await run(context)
    assert result.is_error and payload["status"] == "approval_denied"
    unchanged(old)
    changed.assert_not_called()
    assert not list(old.parent.glob("INVALID_*"))


async def test_default_create_reports_stored_error_before_proposal_schema(setup):
    context, old, approval, changed = setup
    _, payload = await run(context, replace_invalid=False, input_schema={"wrong": True})
    assert payload["status"] == "existing_artifact_invalid"
    assert payload["manifest_path"] == str(old / "manifest.json")
    assert "manifest JSON" in payload["reason"]
    approval.assert_not_called()
    unchanged(old)


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_schema": {}},
        {"input_schema": {"text": "string"}},
        {"input_schema": {"type": "string"}},
        {"input_schema": {"type": ["object", "null"]}},
        {"input_schema": {"type": "object", "properties": {"x": {"type": "bad"}}}},
        {"input_schema": {"type": "object", "properties": {"x": "string"}}},
        {"input_schema": {"type": "object", "$schema": []}},
        {"input_schema": {"type": "object", "$schema": "https://example.invalid/schema"}},
        {"input_schema": {"type": "object", "properties": {"x": {"required": [1]}}}},
        {"input_schema": {"type": "object", "required": "x"}},
        {"command": []},
        {"command": [" "]},
        {"files": {}},
        {"files": {"../escape": "x"}},
        {"files": {"/absolute": "x"}},
        {"files": {"manifest.json": "{}"}},
        {"files": {"x/../../escape": "x"}},
        {"files": {"broken\0": "x"}},
        {"files": {".": "x"}},
        {"files": {"x": "text", "x/y": "text"}},
        {"replace_invalid": "true"},
        {"name": "../helper"},
    ],
)
async def test_invalid_proposal_is_never_installed_and_approved_archive_remains(setup, overrides):
    context, old, approval, changed = setup
    result, payload = await run(context, **overrides)
    assert result.is_error
    changed.assert_not_called()
    if "name" in overrides or "replace_invalid" in overrides:
        approval.assert_not_called()
        unchanged(old)
    else:
        approval.assert_awaited_once()
        assert payload["status"] == "validation_error"
        assert payload["installed"] is False and payload["committed"] is False
        archive = Path(payload["archive_path"])
        unchanged(archive)
        assert not old.exists()
        assert context.tool_registry.get("helper") is None


@pytest.mark.parametrize("change", ["valid", "invalid", "symlink", "missing"])
async def test_changed_target_during_approval_never_replaced(setup, tmp_path, change):
    context, old, approval, changed = setup

    async def approve(*args):
        if change == "valid":
            (old / "manifest.json").write_text(json.dumps(proposal()))
        elif change == "invalid":
            (old / "manifest.json").write_text("[]")
        else:
            old.rename(tmp_path / "saved")
            if change == "symlink":
                symlink(old, tmp_path / "saved")
        return True

    approval.side_effect = approve
    result, _ = await run(context)
    assert result.is_error
    assert not list(old.parent.glob("INVALID_*"))
    changed.assert_not_called()
    assert not list(old.parent.glob(".harness-stage-*"))


@pytest.mark.parametrize("kind", ["target", "broken", "manifest"])
async def test_symlink_targets_and_contents_protected(setup, tmp_path, kind):
    context, old, approval, _ = setup
    if kind in ("target", "broken"):
        old.rename(tmp_path / "saved")
        symlink(old, tmp_path / ("saved" if kind == "target" else "missing"))
    elif kind == "manifest":
        (old / "manifest.json").rename(tmp_path / "manifest")
        symlink(old / "manifest.json", tmp_path / "manifest")
    result, _ = await run(context)
    assert result.is_error
    approval.assert_not_called()
    assert not list(old.parent.glob("INVALID_*"))


async def test_unique_archive_suffix_does_not_overwrite(setup):
    context, old, _, _ = setup
    (old.parent / "INVALID_helper").mkdir()
    symlink(old.parent / "INVALID_helper_1", old.parent / "absent")
    _, payload = await run(context)
    assert payload["archive_path"] == str(old.parent / "INVALID_helper_2")
    assert (old.parent / "INVALID_helper").is_dir()
    assert (old.parent / "INVALID_helper_1").is_symlink()


async def test_missing_manifest_can_be_recreated_with_explicit_approval(setup):
    context, old, approval, _ = setup
    (old / "manifest.json").unlink()
    result, payload = await run(context)
    assert not result.is_error
    assert approval.call_args.args[1]["reason"] == "missing manifest.json"
    assert not (Path(payload["archive_path"]) / "manifest.json").exists()
    assert (Path(payload["archive_path"]) / "original.txt").read_text() == "preserve me"


async def test_install_failure_reports_and_retains_archive(setup, monkeypatch):
    context, old, _, changed = setup

    def rename(source, destination):
        if source == old:
            rename_exclusive(source, destination)
        else:
            raise OSError("simulated failure")

    monkeypatch.setattr(tool_authoring, "rename_exclusive", rename)
    result, payload = await run(context)
    assert result.is_error and payload["status"] == "authoring_failed"
    assert payload["archive_path"] == str(old.parent / "INVALID_helper")
    assert (Path(payload["archive_path"]) / "original.txt").read_text() == "preserve me"
    assert not old.exists()
    changed.assert_not_called()


async def test_approval_exception_preserves_original_and_cleans_staging(setup):
    context, old, approval, changed = setup
    approval.side_effect = RuntimeError("confirmation unavailable")
    result, payload = await run(context)
    assert result.is_error and payload["status"] == "authoring_failed"
    unchanged(old)
    changed.assert_not_called()


async def test_archive_collision_created_during_approval_is_preserved(setup):
    context, old, approval, _ = setup

    async def approve(*args):
        (old.parent / "INVALID_helper").mkdir()
        return True

    approval.side_effect = approve
    result, _ = await run(context)
    assert result.is_error
    unchanged(old)
    assert list((old.parent / "INVALID_helper").iterdir()) == []


@pytest.mark.parametrize("occupied", [False, True])
async def test_failed_install_never_restores_original(setup, monkeypatch, occupied):
    context, old, _, changed = setup
    calls = []

    def rename(source, destination):
        calls.append((source, destination))
        if ".harness-stage-" in str(source):
            if occupied:
                destination.mkdir()
                (destination / "other").write_text("external")
            raise OSError("simulated installation failure")
        rename_exclusive(source, destination)

    monkeypatch.setattr(tool_authoring, "rename_exclusive", rename)
    result, payload = await run(context)
    assert result.is_error and payload["status"] == "authoring_failed"
    assert payload["committed"] is False and payload["installed"] is False
    changed.assert_not_called()
    assert (Path(payload["archive_path"]) / "original.txt").read_text() == "preserve me"
    assert len(calls) == 2
    assert not list(old.parent.glob(".harness-stage-*"))
    if occupied:
        assert (old / "other").read_text() == "external"
    else:
        assert not old.exists()


async def test_missing_valid_and_native_are_not_replaceable(setup):
    context, old, approval, _ = setup
    _, payload = await run(context, name="absent")
    assert payload["status"] == "not_installed"
    _, payload = await run(context, name="bash")
    assert payload["status"] == "already_installed"
    (old / "manifest.json").write_text(json.dumps(proposal()))
    _, payload = await run(context)
    assert payload["status"] == "already_installed"
    approval.assert_not_called()


@pytest.mark.parametrize("manifest", [None, "{", "[]", "{}", "\ufffd"])
async def test_catalog_distinguishes_absent_and_invalid_without_mutating(setup, manifest):
    context, old, _, _ = setup
    (old / "manifest.json").unlink()
    if manifest is not None:
        (old / "manifest.json").write_text(manifest)
    with pytest.raises(KeyError):
        context.tool_catalog["absent"]
    with pytest.raises(InvalidArtifactError) as error:
        context.tool_catalog["helper"]
    assert error.value.name == "helper" and error.value.path == old
    result = await context.tool_catalog["list_available_tools"].execute(context, {})
    entry = json.loads(result.content)["errors"][0]
    assert entry["state"] == "invalid" and entry["path"] == str(old)
    result = await context.tool_catalog["request_tool"].execute(
        context, {"name": "helper", "reason": "test"}
    )
    assert json.loads(result.content)["status"] == "existing_artifact_invalid"
    assert context.tool_registry.get("helper") is None
    assert set(path.name for path in old.parent.iterdir()) == {"helper"}


async def test_per_artifact_lock_serializes_contexts_without_global_approval_lock(setup):
    context, old, approval, changed = setup
    entered = asyncio.Event()
    release = asyncio.Event()

    async def approve(*args):
        entered.set()
        await release.wait()
        return True

    approval.side_effect = approve
    first = asyncio.create_task(run(context))
    await entered.wait()
    second = asyncio.create_task(run(replace(context, tool_registry=ToolRegistry())))
    other, _ = await asyncio.wait_for(run(context, name="other", replace_invalid=False), 2)
    assert not other.is_error
    release.set()
    results = await asyncio.gather(first, second)
    assert [payload["status"] for _, payload in results] == ["recreated", "already_installed"]
    approval.assert_awaited_once()
    changed.assert_called_once()


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O failure")])
@pytest.mark.parametrize("operation", ["read_text", "lstat"])
async def test_operational_errors_are_not_invalid_or_replacement_eligible(
    setup,
    monkeypatch,
    error,
    operation,
):
    context, old, approval, _ = setup
    original = getattr(Path, operation)

    def fail(path, *args, **kwargs):
        if path == (old / "manifest.json" if operation == "read_text" else old):
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail)
    with pytest.raises(type(error)):
        load_artifact_tool(old.parent, "helper")
    result, payload = await run(context)
    assert result.is_error and payload["status"] == "authoring_failed"
    approval.assert_not_called()
    assert not list(old.parent.glob("INVALID_*"))


async def test_special_manifest_is_rejected_without_reading_or_waiting(setup, monkeypatch):
    context, old, approval, _ = setup
    manifest = old / "manifest.json"
    manifest.unlink()
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    try:
        os.mkfifo(manifest)
    except OSError:
        pytest.skip("FIFO creation is unavailable")
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=AssertionError("must not read FIFO")))
    with pytest.raises(InvalidArtifactError, match="regular file"):
        load_artifact_tool(old.parent, "helper")
    result, _ = await run(context)
    assert result.is_error
    approval.assert_not_called()


async def test_auxiliary_files_are_preserved_without_recursive_inspection(setup):
    context, old, approval, _ = setup

    async def approve(*args):
        (old / "original.txt").write_text("updated during approval")
        return True

    approval.side_effect = approve
    result, payload = await run(context)
    assert not result.is_error
    assert (Path(payload["archive_path"]) / "original.txt").read_text() == "updated during approval"


def test_cleanup_retains_changed_and_unknown_staging_files(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    owned = OwnedStaging(stage)
    owned.write(Path("helper/main.py"), "original")
    (stage / "helper/main.py").write_text("changed")
    (stage / "unknown").write_text("preserve")
    assert owned.cleanup()
    assert (stage / "helper/main.py").read_text() == "changed"
    assert (stage / "unknown").read_text() == "preserve"


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_portable_rename_refuses_occupied_destination(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    if kind == "directory":
        destination.mkdir()
    elif kind == "file":
        destination.write_text("preserve")
    else:
        symlink(destination, tmp_path / "missing")
    with pytest.raises(FileExistsError):
        rename_exclusive(source, destination)
    assert source.is_dir()
    assert destination.exists() or destination.is_symlink()


async def test_callback_mutating_proposal_cannot_install_different_payload(setup):
    context, old, approval, _ = setup

    async def approve(definition, details):
        details["replacement"]["files"]["main.py"] = "different proposal"
        return True

    approval.side_effect = approve
    result, payload = await run(context)
    assert result.is_error and payload["status"] == "target_changed"
    unchanged(old)


@pytest.mark.parametrize("recreating", [False, True])
@pytest.mark.parametrize(
    "filename",
    [
        "C:escape",
        "\\escape",
        "\\\\server\\share",
        "a\\..\\escape",
        "stream:name",
        "CON",
        "NUL.txt",
        "COM1.py",
        "name.",
        "name ",
        "MANIFEST.JSON",
        "Manifest.Json/child",
        "../escape",
        "/absolute",
    ],
)
async def test_creation_and_recreation_share_portable_path_validation(
    setup,
    recreating,
    filename,
):
    context, old, approval, changed = setup
    result, payload = await run(
        context,
        name="helper" if recreating else "fresh",
        replace_invalid=recreating,
        files={filename: "invalid path"},
    )
    assert result.is_error and payload["status"] == "validation_error"
    changed.assert_not_called()
    assert not (old.parent / "fresh").exists()
    if recreating:
        approval.assert_awaited_once()
        unchanged(Path(payload["archive_path"]))
        assert not old.exists()
    else:
        approval.assert_not_called()
        unchanged(old)


@pytest.mark.parametrize("name", ["con", "aux", "nul", "prn", "com1", "lpt9"])
async def test_reserved_artifact_names_are_rejected_before_approval(setup, name):
    context, old, approval, _ = setup
    result, payload = await run(context, name=name)
    assert result.is_error and payload["status"] == "validation_error"
    approval.assert_not_called()
    unchanged(old)


async def test_invalid_proposal_denial_performs_no_validation_or_staging(setup, monkeypatch):
    context, old, approval, _ = setup
    approval.return_value = False
    monkeypatch.setattr(
        tool_authoring,
        "validate_artifact_definition",
        Mock(side_effect=AssertionError("must not validate denied replacement")),
    )
    result, payload = await run(context, input_schema={}, files={"../escape": "x"})
    assert result.is_error and payload["status"] == "approval_denied"
    unchanged(old)
    assert not list(old.parent.glob("INVALID_*"))


@pytest.mark.parametrize("mode", ["create", "recover_cached", "recover_cold"])
async def test_authoring_validates_replacement_once_and_reuses_cached_invalid(
    setup,
    monkeypatch,
    mode,
):
    context, old, _, _ = setup
    if mode != "create":
        (old / "manifest.json").write_text(json.dumps(proposal(input_schema={})))
        if mode == "recover_cached":
            with pytest.raises(InvalidArtifactError):
                context.tool_catalog["helper"]
    validate = Mock(wraps=tool_authoring.validate_artifact_definition)
    monkeypatch.setattr(tool_authoring, "validate_artifact_definition", validate)
    monkeypatch.setattr("harness.tools.loader.validate_artifact_definition", validate)
    result, payload = await run(
        context,
        name="fresh" if mode == "create" else "helper",
        replace_invalid=mode != "create",
    )
    assert not result.is_error
    assert payload["status"] == ("created" if mode == "create" else "recreated")
    replacement_calls = [call for call in validate.call_args_list if call.args[1] != {}]
    assert len(replacement_calls) == 1
    assert validate.call_count == (2 if mode == "recover_cold" else 1)


@pytest.mark.parametrize("save", [False, True])
@pytest.mark.parametrize("recreating", [False, True])
async def test_real_persister_reuses_validated_install_even_when_save_declined(
    setup,
    monkeypatch,
    save,
    recreating,
):
    context, old, _, changed = setup
    name = "helper" if recreating else "fresh"
    profiles = context.working_directory / "profiles.json"
    profiles.write_text(
        json.dumps(
            {
                "profiles": {
                    "builder": {
                        "tools": [],
                        "tool_artifacts": [],
                    }
                }
            }
        )
    )
    save_approval = AsyncMock(return_value=save)
    context = replace(
        context,
        persist_tool_access=make_profile_tool_access_persister(
            profiles,
            "builder",
            save_approval,
            context.tool_catalog,
        ),
    )

    def refresh(tool):
        assert context.tool_catalog[name] is tool

    changed.side_effect = refresh
    validate = Mock(wraps=tool_authoring.validate_artifact_definition)
    monkeypatch.setattr(tool_authoring, "validate_artifact_definition", validate)
    monkeypatch.setattr("harness.tools.loader.validate_artifact_definition", validate)
    result, payload = await run(context, name=name, replace_invalid=recreating)
    assert not result.is_error
    assert payload["status"] == ("recreated" if recreating else "created")
    save_approval.assert_awaited_once()
    assert validate.call_count == 1
    assert context.tool_catalog[name] is context.tool_registry.get(name)
    assert validate.call_count == 1
    assigned = json.loads(profiles.read_text())["profiles"]["builder"]["tool_artifacts"]
    assert assigned == ([name] if save else [])

    # Seeding must still allow the normal metadata-based invalidation on edits.
    manifest = old.parent / name / "manifest.json"
    definition = json.loads(manifest.read_text())
    definition["description"] = "Changed after installation and persistence"
    manifest.write_text(json.dumps(definition))
    assert context.tool_catalog[name].definition.description == definition["description"]
    assert validate.call_count == 2


@pytest.mark.parametrize("failure", ["construct", "register", "refresh", "persist"])
async def test_committed_install_errors_report_archive_and_installed_state(
    setup,
    monkeypatch,
    failure,
):
    context, old, approval, changed = setup
    if failure == "construct":
        monkeypatch.setattr(
            tool_authoring,
            "ToolDefinition",
            Mock(
                side_effect=[
                    tool_authoring.ToolDefinition("recreate_invalid_artifact", "approval", {}),
                    ValueError("post-install construction failed"),
                ]
            ),
        )
    elif failure == "register":
        monkeypatch.setattr(
            context.tool_registry, "register", Mock(side_effect=ValueError("register failed"))
        )
    elif failure == "refresh":
        changed.side_effect = RuntimeError("refresh failed")
    else:
        context = replace(
            context, persist_tool_access=AsyncMock(side_effect=OSError("persist failed"))
        )
    result, payload = await run(context)
    assert result.is_error and payload["status"] == "post_install_failed"
    assert payload["committed"] is True and payload["installed"] is True
    assert payload["execution_verified"] is False
    assert payload["path"] == str(old)
    archive = Path(payload["archive_path"])
    assert (archive / "original.txt").read_text() == "preserve me"
    assert (old / "main.py").read_text() == proposal()["files"]["main.py"]
    assert not list(old.parent.glob(".harness-stage-*"))
