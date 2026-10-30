"""Session preparation authorization is distinct from installation and execution."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall
from harness.permissions import AlwaysAsk, AlwaysDeny
from harness.provider import MockProvider
from harness.subagents import SubagentManager, SubagentProfile
from harness.tools.access import make_tool_execution_approver
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
        "files": {"main.py": "raise AssertionError('must not run')"},
        **changes,
    }


def payload(result):
    return json.loads(result.content)


@pytest.fixture
def context(tmp_path):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    return ToolContext(
        tmp_path,
        artifact_root=catalog.artifact_root,
        tool_catalog=catalog,
        approve_artifact_preparation=AsyncMock(return_value=True),
        approve_artifact_creation=AsyncMock(return_value=True),
    )


@pytest.mark.parametrize("extra", [{}, {"authorized": True}, {"replace_invalid": False}])
async def test_unsolicited_full_proposal_never_validates_prompts_or_stages(
    context, monkeypatch, extra
):
    validate = Mock(side_effect=AssertionError("must not validate"))
    monkeypatch.setattr(tool_authoring, "validate_artifact_definition", validate)
    result = await tool_authoring.execute(context, proposal(input_schema={}, **extra))
    assert result.is_error and payload(result)["status"] == "preparation_required"
    validate.assert_not_called()
    context.approve_artifact_preparation.assert_not_awaited()
    context.approve_artifact_creation.assert_not_awaited()
    assert not context.artifact_root.exists()


@pytest.mark.parametrize("callback", ["absent", "deny"])
async def test_preparation_fails_closed_even_without_installation_callback(context, callback):
    context = replace(
        context,
        approve_artifact_preparation=None
        if callback == "absent"
        else AsyncMock(return_value=False),
        approve_artifact_creation=None,
        archived_artifacts={"helper": "diagnostics-only"},
    )
    assert (
        payload(await tool_authoring.execute(context, {"name": "helper"}))["status"]
        == "approval_denied"
    )
    assert (
        payload(await tool_authoring.execute(context, proposal()))["status"]
        == "preparation_required"
    )
    assert not context.artifact_authoring_intents
    assert not context.artifact_root.exists()


async def test_provider_generates_definition_only_after_preparation_callback(context):
    authoring = context.tool_catalog["tool_authoring"]
    sequence = []

    async def prepare(definition, arguments):
        sequence.append("preparation")
        assert definition.name == "prepare_artifact"
        assert arguments == {
            "name": "helper",
            "artifact_root": str(context.artifact_root),
            "working_directory": str(context.working_directory),
        }
        assert not context.artifact_root.exists()
        return True

    async def install(definition, arguments):
        sequence.append("installation")
        assert definition.name == "tool_authoring"
        assert arguments == proposal()
        assert not context.artifact_root.exists()
        return True

    context.approve_artifact_preparation.side_effect = prepare
    context.approve_artifact_creation.side_effect = install

    class Provider(MockProvider):
        async def send(self, messages, definitions):
            if len(self.calls) == 1:
                assert sequence == ["preparation"]
                assert payload(messages[-1].content[0])["status"] == "preparation_authorized"
                context.approve_artifact_creation.assert_not_awaited()
                # Construct the proposal only on the model turn after consent.
                self.responses.append(
                    ProviderResponse(
                        [ToolCall("install", "tool_authoring", proposal())], "tool_use"
                    )
                )
            elif len(self.calls) == 2:
                assert sequence == ["preparation", "installation"]
                self.responses.append(ProviderResponse([TextBlock("Created")], "end_turn"))
            return await super().send(messages, definitions)

    provider = Provider(
        [ProviderResponse([ToolCall("intent", "tool_authoring", {"name": "helper"})], "tool_use")]
    )
    generic = AsyncMock(side_effect=AssertionError("native handler owns prompts"))
    agent = Agent(
        provider,
        ToolRegistry([authoring]),
        AlwaysAsk(make_tool_execution_approver(generic, [authoring])),
        context,
    )
    assert await agent.send("Create helper") == "Created"
    assert sequence == ["preparation", "installation"]
    assert not context.artifact_authoring_intents
    assert (context.artifact_root / "helper" / "manifest.json").exists()


async def test_validation_correction_retains_intent_and_only_valid_proposal_requests_installation(
    context,
):
    assert (
        payload(await tool_authoring.execute(context, {"name": "helper"}))["status"]
        == "preparation_authorized"
    )
    assert not context.artifact_root.exists()
    invalid = await tool_authoring.execute(context, proposal(input_schema={}))
    assert payload(invalid)["status"] == "validation_error"
    assert context.artifact_authoring_intents
    assert not context.artifact_root.exists()
    corrected = await tool_authoring.execute(replace(context, tool_name="author_alias"), proposal())
    assert payload(corrected)["status"] == "created"
    assert context.approve_artifact_preparation.await_count == 1
    assert context.approve_artifact_creation.await_count == 1
    assert not context.artifact_authoring_intents


@pytest.mark.parametrize("outcome", ["deny", "cancel", "error"])
async def test_installation_denial_cancellation_and_failure_consume_intent(context, outcome):
    await tool_authoring.execute(context, {"name": "helper"})
    if outcome == "deny":
        context.approve_artifact_creation.return_value = False
    else:
        context.approve_artifact_creation.side_effect = (
            asyncio.CancelledError() if outcome == "cancel" else OSError("failed")
        )
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await tool_authoring.execute(context, proposal())
    else:
        assert (await tool_authoring.execute(context, proposal())).is_error
    assert not context.artifact_authoring_intents
    assert (
        payload(await tool_authoring.execute(context, proposal()))["status"]
        == "preparation_required"
    )
    assert context.approve_artifact_creation.await_count == 1
    assert not context.artifact_root.exists()


@pytest.mark.parametrize("change", ["session", "root", "workspace", "name"])
async def test_intent_does_not_transfer_to_other_session_or_target(context, change, tmp_path):
    await tool_authoring.execute(context, {"name": "helper"})
    arguments = proposal()
    other = context
    if change == "session":
        other = ToolContext(
            tmp_path,
            artifact_root=context.artifact_root,
            approve_artifact_creation=context.approve_artifact_creation,
        )
    elif change == "root":
        other = replace(context, artifact_root=tmp_path / "other", tool_catalog=None)
    elif change == "workspace":
        other = replace(context, working_directory=tmp_path / "other")
    else:
        arguments["name"] = "other"
    assert (
        payload(await tool_authoring.execute(other, arguments))["status"] == "preparation_required"
    )
    context.approve_artifact_creation.assert_not_awaited()
    assert not context.artifact_root.exists()


@pytest.mark.parametrize("full", [False, True])
async def test_installed_observation_clears_pending_intent_without_prompts(context, full):
    await tool_authoring.execute(context, {"name": "helper"})
    directory = context.artifact_root / "helper"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(proposal()))
    result = await tool_authoring.execute(
        context, proposal(input_schema={}) if full else {"name": "helper"}
    )
    assert payload(result)["status"] == "already_installed"
    assert not context.artifact_authoring_intents
    assert context.approve_artifact_preparation.await_count == 1
    context.approve_artifact_creation.assert_not_awaited()
    directory.rename(context.artifact_root / "removed")
    assert (
        payload(await tool_authoring.execute(context, proposal()))["status"]
        == "preparation_required"
    )


@pytest.mark.parametrize("full", [False, True])
async def test_invalid_target_requires_archive_gate_even_with_preparation_callback(context, full):
    directory = context.artifact_root / "helper"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text("{")
    result = await tool_authoring.execute(context, proposal() if full else {"name": "helper"})
    assert payload(result)["status"] == "existing_artifact_invalid"
    assert "only name and replace_invalid=true" in payload(result)["next_step"]
    context.approve_artifact_preparation.assert_not_awaited()
    context.approve_artifact_creation.assert_not_awaited()
    assert not context.artifact_authoring_intents


async def test_native_policy_denial_precedes_preparation_consent(context):
    authoring = context.tool_catalog["tool_authoring"]
    provider = MockProvider(
        [
            ProviderResponse(
                [ToolCall("intent", "tool_authoring", {"name": "helper"})], "tool_use"
            ),
            ProviderResponse([TextBlock("Denied")], "end_turn"),
        ]
    )
    await Agent(provider, ToolRegistry([authoring]), AlwaysDeny(), context).send("Create")
    assert payload(provider.calls[-1][0][-1].content[0])["status"] == "access_denied"
    context.approve_artifact_preparation.assert_not_awaited()
    assert not context.artifact_authoring_intents


async def test_committed_installation_consumes_intent_even_when_persistence_fails(context):
    context = replace(context, persist_tool_access=AsyncMock(side_effect=OSError("failed")))
    await tool_authoring.execute(context, {"name": "helper"})
    result = await tool_authoring.execute(context, proposal())
    assert payload(result)["status"] == "post_install_failed"
    assert payload(result)["committed"] is True
    assert not context.artifact_authoring_intents


async def test_fresh_delegation_cannot_consume_previous_preparation_intent(context):
    authoring = context.tool_catalog["tool_authoring"]
    provider = MockProvider(
        [
            ProviderResponse(
                [ToolCall("prepare", "tool_authoring", {"name": "helper"})], "tool_use"
            ),
            ProviderResponse([TextBlock("Prepared")], "end_turn"),
            ProviderResponse([ToolCall("install", "tool_authoring", proposal())], "tool_use"),
            ProviderResponse([TextBlock("Installed")], "end_turn"),
        ]
    )
    created = []

    def factory(profile):
        agent = Agent(
            provider,
            ToolRegistry(profile.tools),
            AlwaysAsk(make_tool_execution_approver(AsyncMock(return_value=True), [authoring])),
            replace(context, artifact_authoring_intents=set(), archived_artifacts={}),
        )
        created.append(agent)
        return agent

    profile = SubagentProfile("lab", "Lab", "Lab", tools=(authoring,))
    manager = SubagentManager(factory, {"lab": profile})
    assert (await manager.delegate("lab", "Prepare")).result == "Prepared"
    assert created[0].context.artifact_authoring_intents
    assert not context.artifact_authoring_intents
    assert not context.artifact_root.exists()
    assert (await manager.delegate("lab", "Install")).result == "Installed"
    assert len(created) == 2
    assert context.approve_artifact_preparation.await_count == 1
    assert context.approve_artifact_creation.await_count == 0
    result = created[1].provider.calls[-1][0][-1].content[0]
    assert result.is_error and payload(result)["status"] == "preparation_required"
    assert created[0].context.artifact_authoring_intents
    assert not created[1].context.artifact_authoring_intents
    assert not context.artifact_authoring_intents


async def test_missing_installation_callback_denies_after_valid_preparation(context, monkeypatch):
    context = replace(context, approve_artifact_creation=None)
    prepared = await tool_authoring.execute(context, {"name": "helper"})
    assert payload(prepared)["status"] == "preparation_authorized"
    assert context.artifact_authoring_intents
    validate = Mock(side_effect=AssertionError("must not validate without installation control"))
    monkeypatch.setattr(tool_authoring, "validate_artifact_definition", validate)
    result = await tool_authoring.execute(context, proposal())
    assert result.is_error and payload(result)["status"] == "approval_denied"
    assert "approval unavailable" in payload(result)["message"]
    validate.assert_not_called()
    assert not context.artifact_authoring_intents
    assert not context.artifact_root.exists()


@pytest.mark.parametrize("state", ["valid", "invalid", "missing_manifest"])
@pytest.mark.parametrize("root_existed", [False, True])
async def test_preparation_rechecks_target_appearing_during_approval(context, state, root_existed):
    if root_existed:
        context.artifact_root.mkdir()
    directory = context.artifact_root / "helper"

    async def approve(definition, details):
        assert details == {
            "name": "helper",
            "artifact_root": str(context.artifact_root),
            "working_directory": str(context.working_directory),
        }
        directory.mkdir(parents=True)
        if state != "missing_manifest":
            (directory / "manifest.json").write_text(
                json.dumps(proposal()) if state == "valid" else "{"
            )
        return True

    context.approve_artifact_preparation.side_effect = approve
    result = await tool_authoring.execute(context, {"name": "helper"})
    assert payload(result)["status"] == (
        "already_installed" if state == "valid" else "existing_artifact_invalid"
    )
    assert result.is_error is (state != "valid")
    assert not context.artifact_authoring_intents
    assert directory.exists()
    context.approve_artifact_creation.assert_not_awaited()
    assert not list(context.artifact_root.glob(".harness-stage-*"))


@pytest.mark.parametrize(
    "change", ["replace_root", "create_root", "mutate_details", "symlink_root"]
)
async def test_preparation_rejects_changed_root_or_scope_before_grant(context, change, tmp_path):
    root = context.artifact_root
    if change != "create_root":
        root.mkdir()

    async def approve(definition, details):
        assert details["artifact_root"] == str(root)
        assert details["working_directory"] == str(tmp_path)
        if change == "mutate_details":
            details["artifact_root"] = str(tmp_path / "other")
        elif change == "create_root":
            root.mkdir()
        else:
            root.rename(tmp_path / "preserved_root")
            if change == "symlink_root":
                root.symlink_to(tmp_path / "preserved_root", target_is_directory=True)
            else:
                root.mkdir()
        return True

    context.approve_artifact_preparation.side_effect = approve
    result = await tool_authoring.execute(context, {"name": "helper"})
    assert result.is_error
    assert payload(result)["status"] == (
        "protected" if change == "symlink_root" else "target_changed"
    )
    assert not context.artifact_authoring_intents
    context.approve_artifact_creation.assert_not_awaited()
    assert not (root / "helper").exists()
