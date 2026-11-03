"""Preparation consent is reusable only while its scoped intent remains valid."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from harness.tools.base import ToolContext
from harness.tools.handlers import tool_authoring


def proposal(**changes):
    return {
        "name": "helper",
        "description": "Helper",
        "input_schema": {"type": "object"},
        "command": ["never-execute", "{artifact_dir}/main.py"],
        "files": {"main.py": "raise AssertionError('must not execute')"},
        **changes,
    }


def status(result):
    return json.loads(result.content)["status"]


@pytest.fixture
def context(tmp_path):
    return ToolContext(
        tmp_path,
        artifact_root=tmp_path / "artifacts",
        approve_artifact_preparation=AsyncMock(return_value=True),
        approve_artifact_replacement=AsyncMock(return_value=True),
        approve_artifact_creation=AsyncMock(return_value=True),
        persist_tool_access=AsyncMock(return_value=True),
    )


async def prepare(context, source):
    if source == "archive":
        directory = context.artifact_root / "helper"
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text("{")
        (directory / "original.txt").write_text("preserve me")
        result = await tool_authoring.execute(context, {"name": "helper", "replace_invalid": True})
        assert status(result) == "original_archived"
    else:
        result = await tool_authoring.execute(context, {"name": "helper"})
        assert status(result) == "preparation_authorized"
    assert not result.is_error


@pytest.mark.parametrize("source", ["ordinary", "archive"])
@pytest.mark.parametrize("explicit_false", [False, True])
async def test_repeated_preparation_is_noop(context, source, explicit_false, monkeypatch):
    await prepare(context, source)
    intents = context.artifact_authoring_intents.copy()
    preparation_calls = context.approve_artifact_preparation.await_count
    replacement_calls = context.approve_artifact_replacement.await_count
    validate = Mock(side_effect=AssertionError("must not validate a proposal"))
    staging = Mock(side_effect=AssertionError("must not create staging files"))
    monkeypatch.setattr(tool_authoring, "validate_artifact_definition", validate)
    monkeypatch.setattr(tool_authoring.tempfile, "mkdtemp", staging)
    arguments = {"name": "helper"}
    if explicit_false:
        arguments["replace_invalid"] = False
    for _ in range(2):
        result = await tool_authoring.execute(context, arguments)
        assert not result.is_error
        assert status(result) == "preparation_authorized"
        assert json.loads(result.content)["committed"] is False
    # A live intent also works when the UI callback is no longer available.
    result = await tool_authoring.execute(
        replace(context, approve_artifact_preparation=None), arguments
    )
    assert not result.is_error and status(result) == "preparation_authorized"
    assert context.artifact_authoring_intents == intents
    assert context.approve_artifact_preparation.await_count == preparation_calls
    assert context.approve_artifact_replacement.await_count == replacement_calls
    context.approve_artifact_creation.assert_not_awaited()
    context.persist_tool_access.assert_not_awaited()
    validate.assert_not_called()
    staging.assert_not_called()
    assert not (context.artifact_root / "helper").exists()
    if source == "archive":
        # Use the actual diagnostic path; its presence alone is never consent.
        archive = Path(context.archived_artifacts["helper"])
        assert (archive / "manifest.json").read_text() == "{"
        assert (archive / "original.txt").read_text() == "preserve me"
        assert list(context.artifact_root.iterdir()) == [archive]
    else:
        assert not context.artifact_root.exists()


@pytest.mark.parametrize("change", ["context", "root", "workspace", "name", "diagnostics"])
async def test_no_reuse_outside_matching_intent(context, change):
    await prepare(context, "archive")
    other = replace(context, approve_artifact_preparation=None)
    arguments = {"name": "helper"}
    if change == "context":
        other = replace(other, artifact_authoring_intents=set())
    elif change == "root":
        other = replace(other, artifact_root=context.working_directory / "other")
    elif change == "workspace":
        other = replace(other, working_directory=context.working_directory / "other")
    elif change == "name":
        arguments["name"] = "other"
    else:
        context.artifact_authoring_intents.clear()
    result = await tool_authoring.execute(other, arguments)
    assert result.is_error and status(result) == "approval_denied"
    context.approve_artifact_preparation.assert_not_awaited()
    context.approve_artifact_creation.assert_not_awaited()


@pytest.mark.parametrize("source", ["ordinary", "archive"])
@pytest.mark.parametrize("target", ["valid", "invalid"])
async def test_target_recheck_precedes_reuse(context, source, target):
    await prepare(context, source)
    directory = context.artifact_root / "helper"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(proposal()) if target == "valid" else "{")
    calls = context.approve_artifact_preparation.await_count
    result = await tool_authoring.execute(context, {"name": "helper"})
    assert status(result) == (
        "already_installed" if target == "valid" else "existing_artifact_invalid"
    )
    assert not context.artifact_authoring_intents
    assert context.approve_artifact_preparation.await_count == calls
    context.approve_artifact_creation.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["deny", "cancel", "validation"])
async def test_installation_result_controls_preparation_reuse(context, outcome):
    await prepare(context, "archive")
    arguments = proposal()
    if outcome == "deny":
        context.approve_artifact_creation.return_value = False
    elif outcome == "cancel":
        context.approve_artifact_creation.side_effect = asyncio.CancelledError()
    else:
        arguments["input_schema"] = {}
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await tool_authoring.execute(context, arguments)
    else:
        result = await tool_authoring.execute(context, arguments)
        assert result.is_error
        assert status(result) == (
            "validation_error" if outcome == "validation" else "approval_denied"
        )
    assert bool(context.artifact_authoring_intents) == (outcome == "validation")
    result = await tool_authoring.execute(context, {"name": "helper"})
    assert not result.is_error and status(result) == "preparation_authorized"
    assert context.approve_artifact_preparation.await_count == (outcome != "validation")
    assert context.approve_artifact_creation.await_count == (outcome != "validation")
    context.persist_tool_access.assert_not_awaited()
    assert not (context.artifact_root / "helper").exists()
