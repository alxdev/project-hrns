"""Validate before installation consent; correct within one scripted child loop."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall
from harness.permissions import AlwaysAllow
from harness.provider import MockProvider
from harness.subagents import SubagentManager, SubagentProfile
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.handlers import tool_authoring
from harness.tools.loader import load_tools
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


def proposal(**changes):
    return {
        "name": "helper",
        "description": "Helper",
        "input_schema": {"type": "object"},
        "command": ["never-execute", "{artifact_dir}/main.py"],
        "files": {"main.py": "raise AssertionError('creation must not execute')"},
        **changes,
    }


def payload(result):
    return json.loads(result.content)


@pytest.fixture
def context(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("validation and installation must not execute scripts")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr("harness.tools.artifacts.run_process", forbidden)
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    return ToolContext(
        tmp_path,
        artifact_root=catalog.artifact_root,
        tool_catalog=catalog,
        tool_registry=ToolRegistry([catalog["tool_authoring"]]),
        approve_artifact_preparation=AsyncMock(return_value=True),
        approve_artifact_replacement=AsyncMock(return_value=True),
        approve_artifact_creation=AsyncMock(return_value=True),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"description": ""},
        {"input_schema": {}},
        {"input_schema": {"type": "object", "required": "wrong"}},
        {"command": None},
        {"command": []},
        {"command": [" "]},
        {"files": {}},
        {"files": []},
        {"files": {"main.py": 3}},
        {"files": {"../outside": "no"}},
        {"files": {"manifest.json": "no"}},
        {"files": {"main.py": "a", "main.py/child": "b"}},
    ],
)
async def test_invalid_proposal_never_prompts_for_installation_or_writes(context, changes):
    await tool_authoring.execute(context, {"name": "helper"})
    intents = context.artifact_authoring_intents.copy()
    result = await tool_authoring.execute(context, proposal(**changes))
    data = payload(result)
    assert result.is_error and data["status"] == "validation_error"
    assert "same child invocation" in data["next_step"]
    assert "same approved name/root/workspace" in data["next_step"]
    assert "No conversational root reauthorization" in data["next_step"]
    assert data["committed"] is False and data["installed"] is False
    assert context.artifact_authoring_intents == intents
    context.approve_artifact_creation.assert_not_awaited()
    assert not context.artifact_root.exists()


@pytest.mark.parametrize("archive", [False, True])
async def test_single_child_corrects_proposal_with_one_valid_install_prompt(
    context, monkeypatch, archive
):
    if archive:
        original = context.artifact_root / "helper"
        original.mkdir(parents=True)
        (original / "manifest.json").write_text("{", encoding="utf-8")
    validate = Mock(wraps=tool_authoring.validate_artifact_definition)
    portable = Mock(wraps=tool_authoring.portable_component)
    monkeypatch.setattr(tool_authoring, "validate_artifact_definition", validate)
    monkeypatch.setattr(tool_authoring, "portable_component", portable)

    async def approve(_definition, arguments):
        assert validate.call_count == 2  # One invalid attempt, one corrected attempt.
        assert portable.call_count == 1  # Files examined only on the valid attempt.
        assert not (context.artifact_root / "helper").exists()
        assert not list(context.artifact_root.glob(".harness-stage-*"))
        assert arguments["command"] == proposal()["command"]
        # Callback changes must not alter the private, already validated values.
        arguments["files"]["main.py"] = "unapproved callback mutation"
        return True

    context.approve_artifact_creation.side_effect = approve
    invalid = proposal(command=None)
    responses = [
        ProviderResponse(
            [ToolCall("prepare", "tool_authoring", {"name": "helper", "replace_invalid": archive})],
            "tool_use",
        ),
        ProviderResponse([ToolCall("invalid", "tool_authoring", invalid)], "tool_use"),
        ProviderResponse([ToolCall("corrected", "tool_authoring", proposal())], "tool_use"),
        ProviderResponse([TextBlock("Definition installed; not executed.")], "end_turn"),
    ]
    provider = MockProvider(responses)
    child = Agent(provider, context.tool_registry, AlwaysAllow(), context)
    factory = Mock(return_value=child)
    manager = SubagentManager(factory, {"worker": SubagentProfile("worker", "Worker", "Worker")})
    record = await manager.delegate(
        "worker", "Prepare, correct validation errors, and install only"
    )
    factory.assert_called_once()
    assert record.result == "Definition installed; not executed."
    failed = payload(provider.calls[2][0][-1].content[0])
    assert failed["status"] == "validation_error"
    assert "separately authorized" not in failed["next_step"]
    assert "do not retry automatically" not in failed["next_step"]
    assert payload(provider.calls[3][0][-1].content[0])["status"] == "created"
    assert validate.call_count == 2 and portable.call_count == 1
    assert (context.artifact_root / "helper" / "main.py").read_text() == proposal()["files"][
        "main.py"
    ]
    context.approve_artifact_creation.assert_awaited_once()
    assert context.approve_artifact_preparation.await_count == (0 if archive else 1)
    assert context.approve_artifact_replacement.await_count == (1 if archive else 0)
    assert len(list(context.artifact_root.glob("INVALID_helper*"))) == (1 if archive else 0)
    assert not context.artifact_authoring_intents


@pytest.mark.parametrize("outcome", ["deny", "cancel", "error"])
async def test_installation_denial_cancel_and_failure_consume_consent(context, outcome):
    await tool_authoring.execute(context, {"name": "helper"})
    if outcome == "deny":
        context.approve_artifact_creation.return_value = False
    else:
        context.approve_artifact_creation.side_effect = (
            asyncio.CancelledError() if outcome == "cancel" else RuntimeError("approval failed")
        )
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await tool_authoring.execute(context, proposal())
    else:
        data = payload(await tool_authoring.execute(context, proposal()))
        assert data["status"] == ("approval_denied" if outcome == "deny" else "authoring_failed")
        assert "same child invocation" not in data["next_step"]
    assert not context.artifact_authoring_intents
    assert not context.artifact_root.exists()
    result = await tool_authoring.execute(context, proposal())
    assert payload(result)["status"] == "preparation_required"
    context.approve_artifact_creation.assert_awaited_once()


@pytest.mark.parametrize("change", ["target", "root"])
async def test_target_or_root_changes_during_approval_never_overwrite(context, change):
    if change == "target":
        context.artifact_root.mkdir()
    await tool_authoring.execute(context, {"name": "helper"})

    async def approve(_definition, _arguments):
        if change == "root":
            context.artifact_root.mkdir()
            (context.artifact_root / "unrelated.txt").write_text("preserve", encoding="utf-8")
        else:
            occupied = context.artifact_root / "helper"
            occupied.mkdir(parents=True)
            (occupied / "manifest.json").write_text("{", encoding="utf-8")
        return True

    context.approve_artifact_creation.side_effect = approve
    data = payload(await tool_authoring.execute(context, proposal()))
    assert data["status"] == ("target_changed" if change == "root" else "existing_artifact_invalid")
    assert not context.artifact_authoring_intents
    assert not list(context.artifact_root.glob(".harness-stage-*"))
    assert not (context.artifact_root / "helper" / "main.py").exists()
    if change == "root":
        assert (context.artifact_root / "unrelated.txt").read_text() == "preserve"
    else:
        assert (context.artifact_root / "helper" / "manifest.json").read_text() == "{"


async def test_post_install_failure_reports_committed_state_without_recreation(context):
    await tool_authoring.execute(context, {"name": "helper"})
    context = replace(
        context, persist_tool_access=AsyncMock(side_effect=RuntimeError("save failed"))
    )
    data = payload(await tool_authoring.execute(context, proposal()))
    assert data["status"] == "post_install_failed"
    assert data["installed"] is True and data["committed"] is True
    assert "Do not recreate or retry automatically" in data["recovery_info"]
    assert (context.artifact_root / "helper" / "manifest.json").is_file()
    assert not context.artifact_authoring_intents
