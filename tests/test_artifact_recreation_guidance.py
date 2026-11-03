"""A missing recreation target guides ordinary preparation without granting consent."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harness.tools.artifact_errors import InvalidArtifactError
from harness.tools.base import ToolContext
from harness.tools.handlers import tool_authoring


@pytest.mark.parametrize("entry", ["execute", "stale_recreation"])
async def test_missing_recreation_target_requires_ordinary_preparation(tmp_path, entry):
    context = ToolContext(
        tmp_path,
        artifact_root=tmp_path / "artifacts",
        tool_name="author_alias",
        approve_artifact_replacement=AsyncMock(return_value=True),
        approve_artifact_preparation=AsyncMock(return_value=False),
        approve_artifact_creation=AsyncMock(return_value=True),
        persist_tool_access=AsyncMock(return_value=True),
    )
    if entry == "execute":
        result = await tool_authoring.execute(context, {"name": "helper", "replace_invalid": True})
    else:
        result = await tool_authoring.prepare_recreation(
            context, InvalidArtifactError("helper", context.artifact_root / "helper", "stale")
        )

    payload = json.loads(result.content)
    assert result.is_error and payload["status"] == "not_installed"
    assert "only name" in payload["next_step"]
    assert "Do not repeat archive/recreate or retry automatically" in payload["next_step"]
    assert payload["recovery_action"] == {
        "tool": "author_alias",
        "name": "helper",
        "replace_invalid": False,
    }
    assert not context.artifact_authoring_intents
    assert not context.archived_artifacts
    context.approve_artifact_replacement.assert_not_awaited()
    context.approve_artifact_preparation.assert_not_awaited()
    context.approve_artifact_creation.assert_not_awaited()
    context.persist_tool_access.assert_not_awaited()
    assert not context.artifact_root.exists()

    # Following the guidance still goes through preparation consent and respects denial.
    result = await tool_authoring.execute(context, {"name": "helper"})
    assert result.is_error and json.loads(result.content)["status"] == "approval_denied"
    context.approve_artifact_preparation.assert_awaited_once()
    context.approve_artifact_creation.assert_not_awaited()
    assert not context.artifact_authoring_intents
    assert not context.artifact_root.exists()


async def test_archived_original_is_preserved_when_recreation_is_repeated(tmp_path):
    context = ToolContext(
        tmp_path,
        artifact_root=tmp_path / "artifacts",
        approve_artifact_replacement=AsyncMock(return_value=True),
        approve_artifact_preparation=AsyncMock(return_value=False),
        approve_artifact_creation=AsyncMock(return_value=True),
    )
    original = context.artifact_root / "helper"
    original.mkdir(parents=True)
    (original / "manifest.json").write_text("{", encoding="utf-8")
    archived = await tool_authoring.execute(context, {"name": "helper", "replace_invalid": True})
    archive = Path(json.loads(archived.content)["archive_path"])

    result = await tool_authoring.execute(context, {"name": "helper", "replace_invalid": True})
    assert result.is_error and json.loads(result.content)["status"] == "not_installed"
    assert (archive / "manifest.json").read_text(encoding="utf-8") == "{"
    assert list(context.artifact_root.iterdir()) == [archive]
    assert not original.exists()
    context.approve_artifact_replacement.assert_awaited_once()
    context.approve_artifact_preparation.assert_not_awaited()
    context.approve_artifact_creation.assert_not_awaited()
    assert not context.artifact_authoring_intents
