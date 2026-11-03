import asyncio
import errno
import json
import socket
import subprocess
import sys
import warnings
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harness.agent import Agent
from harness.api.types import ProviderResponse, TextBlock, ToolCall
from harness.permissions import AlwaysAllow
from harness.provider import MockProvider
from harness.subagents import load_profiles
from harness.tools.artifact_validation import validate_artifact_definition
from harness.tools.base import ToolContext
from harness.tools.catalog import ToolCatalog
from harness.tools.loader import load_artifact_tool, load_tools
from harness.tools.process import ProcessResult
from harness.tools.registry import ToolRegistry

TOOLS = Path(__file__).parents[1] / "tools.json"


async def author(tool, context, proposal):
    context = replace(
        context,
        approve_artifact_preparation=AsyncMock(return_value=True),
        approve_artifact_creation=AsyncMock(return_value=True),
    )
    assert not (await tool.execute(context, {"name": proposal["name"]})).is_error
    return await tool.execute(context, proposal)


BAD_SCHEMAS = [
    {"text": "string"},
    {},
    {"type": "string"},
    {"type": ["object", "null"]},
    {"type": "object", "properties": {"text": {"type": "not_a_type"}}},
    {"type": "object", "properties": {"text": "string"}},
    {"type": "object", "required": "text"},
    {"type": "object", "properties": {"nested": {"required": [1]}}},
    {"type": "object", "$schema": []},
    {"type": "object", "$schema": "https://example.invalid/schema"},
]


def arguments(**overrides):
    return {
        "name": "helper",
        "description": "Temporary test tool",
        "input_schema": {"type": "object"},
        "files": {"main.py": "raise AssertionError('creation must not execute')"},
        "command": [sys.executable, "{artifact_dir}/main.py"],
        **overrides,
    }


@pytest.mark.parametrize("schema", BAD_SCHEMAS)
async def test_bad_schema_has_no_creation_registration_or_persistence(tmp_path, schema):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    registry = ToolRegistry()
    persist = AsyncMock()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = await author(
            catalog["tool_authoring"],
            ToolContext(
                tmp_path,
                artifact_root=catalog.artifact_root,
                tool_registry=registry,
                persist_tool_access=persist,
            ),
            arguments(input_schema=schema),
        )
    assert result.is_error
    assert json.loads(result.content)["status"] == "validation_error"
    assert len(result.content) < 700
    assert not catalog.artifact_root.exists()
    assert registry.definitions() == []
    persist.assert_not_called()


@pytest.mark.parametrize("schema", BAD_SCHEMAS)
async def test_invalid_manifest_discovery_request_and_profile_fail_before_provider(
    tmp_path, schema
):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    directory = catalog.artifact_root / "helper"
    directory.mkdir(parents=True)
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps(arguments(input_schema=schema)))
    original = manifest.read_bytes()
    registry = ToolRegistry([catalog["list_available_tools"], catalog["request_tool"]])
    context = ToolContext(tmp_path, tool_registry=registry, tool_catalog=catalog)
    listed = json.loads((await catalog["list_available_tools"].execute(context, {})).content)
    assert all(item["name"] != "helper" for item in listed["tools"])
    assert [item["name"] for item in listed["errors"]] == ["helper"]
    provider = MockProvider(
        [
            ProviderResponse(
                [ToolCall("request", "request_tool", {"name": "helper", "reason": "test"})],
                "tool_use",
            ),
            ProviderResponse([TextBlock("done")], "end_turn"),
        ]
    )
    await Agent(provider, registry, AlwaysAllow(), context).send("request helper")
    assert (
        json.loads(provider.calls[-1][0][-1].content[0].content)["status"]
        == "existing_artifact_invalid"
    )
    assert all(tool.name != "helper" for _, definitions in provider.calls for tool in definitions)
    profile = tmp_path / "profiles.json"
    profile.write_text(
        json.dumps(
            {
                "profiles": {
                    "coder": {
                        "description": "Test",
                        "system_prompt": "Test",
                        "permission_mode": "inherit",
                        "tools": [],
                        "tool_artifacts": ["helper"],
                    }
                }
            }
        )
    )
    loaded = load_profiles(profile, catalog)
    assert all(tool.definition.name != "helper" for tool in loaded["coder"].tools)
    assert manifest.read_bytes() == original


@pytest.mark.parametrize(
    "command",
    [
        [],
        [""],
        ["   "],
        ["runtime\0"],
        ["runtime", "bad\0arg"],
        "runtime",
    ],
)
async def test_bad_command_rejected_before_creation(tmp_path, command):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    result = await author(
        catalog["tool_authoring"],
        ToolContext(tmp_path, artifact_root=catalog.artifact_root),
        arguments(command=command),
    )
    assert result.is_error
    assert json.loads(result.content)["status"] == "validation_error"
    assert not catalog.artifact_root.exists()
    directory = catalog.artifact_root / "helper"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(arguments(command=command)))
    with pytest.raises(ValueError, match="command"):
        load_artifact_tool(catalog.artifact_root, "helper")


@pytest.mark.parametrize(
    "command",
    [
        ["python3", "{artifact_dir}/main.py"],
        ["sh", "{artifact_dir}/run.sh"],
        ["node", "{artifact_dir}/main.js"],
        ["uninstalled-runtime", "{artifact_dir}/main.ext"],
        ["/path with spaces/runtime"],
        ["relative path/runtime", ""],
        ["runtime {artifact_dir}/main.ext"],
    ],
)
async def test_valid_argv_creation_and_load_do_not_execute(tmp_path, command):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    result = await author(
        catalog["tool_authoring"],
        ToolContext(tmp_path, artifact_root=catalog.artifact_root),
        arguments(command=command),
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["execution_verified"] is False
    assert "functional behavior not verified" in payload["message"]
    assert (
        "Functional verification requires a separately approved execution" in payload["next_step"]
    )
    assert load_artifact_tool(catalog.artifact_root, "helper").command == tuple(command)


async def test_validation_never_uses_network_or_executes_code(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("definition validation must not use the network or launch a process")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr("harness.tools.artifacts.run_process", forbidden)
    validate_artifact_definition(
        "Test",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"x": {"$ref": "https://example.invalid/schema"}},
        },
        ["runtime"],
    )
    await create_tool(tmp_path)
    sentinel = "PRIVATE_SCHEMA_SOURCE_SENTINEL"
    with pytest.raises(ValueError) as error:
        validate_artifact_definition(
            "Test",
            {
                "type": "object",
                "properties": {"text": {"type": sentinel}},
                "default": {"private": sentinel},
            },
            ["runtime"],
        )
    assert sentinel not in str(error.value)
    assert len(str(error.value)) < 400


async def create_tool(tmp_path, **overrides):
    catalog = ToolCatalog(load_tools(TOOLS), tmp_path / "artifacts")
    result = await author(
        catalog["tool_authoring"],
        ToolContext(tmp_path, artifact_root=catalog.artifact_root),
        arguments(**overrides),
    )
    assert not result.is_error
    return catalog["helper"]


async def test_json_stdin_stdout(tmp_path):
    tool = await create_tool(
        tmp_path,
        command=[sys.executable, "main.py"],
        files={
            "main.py": (
                "import json,sys\na=json.load(sys.stdin)\n"
                'print(json.dumps({"sum":sum(a["values"])}))'
            ),
        },
    )
    result = await tool.execute(ToolContext(tmp_path), {"values": [2, 3, 5]})
    assert not result.is_error
    assert json.loads(result.content) == {"sum": 10}


async def test_relative_executable_with_space_and_placeholder_is_preserved(tmp_path, monkeypatch):
    tool = await create_tool(
        tmp_path,
        command=["runtime {artifact_dir}/program", "argument with spaces", "", "a&b"],
    )
    expanded = "runtime " + str(tool.directory) + "/program"
    runner = AsyncMock(return_value=ProcessResult("ok", "", 0))
    monkeypatch.setattr("harness.tools.artifacts.run_process", runner)
    result = await tool.execute(ToolContext(tmp_path), {})
    assert not result.is_error
    runner.assert_awaited_once()
    assert runner.call_args.args == ([expanded, "argument with spaces", "", "a&b"],)
    assert runner.call_args.kwargs["cwd"] == tmp_path.resolve()
    assert runner.call_args.kwargs["input_data"] == b"{}"


async def test_missing_executable_and_missing_working_directory_are_distinct(tmp_path, monkeypatch):
    executable = str(tmp_path / "unavailable runtime")
    sentinel = "PRIVATE_ARGUMENT_ENV_SOURCE_SENTINEL"
    monkeypatch.setenv("HARNESS_PRIVATE_TEST", sentinel)
    tool = await create_tool(tmp_path, command=[executable, sentinel], files={"main.py": sentinel})
    result = await tool.execute(ToolContext(tmp_path), {"private": sentinel})
    assert sentinel not in result.content
    payload = json.loads(result.content)
    assert result.is_error and payload["status"] == "executable_unavailable"
    assert payload["executable"] == executable
    assert "separate argv entries" in payload["next_step"]
    result = await tool.execute(ToolContext(tmp_path / "missing workspace"), {})
    assert result.is_error
    assert json.loads(result.content)["status"] == "working_directory_unavailable"


async def test_ambiguous_command_is_reported_verbatim_without_splitting(tmp_path):
    executable = "python3 {artifact_dir}/main.py"
    tool = await create_tool(tmp_path, command=[executable])
    executable = executable.replace("{artifact_dir}", str(tool.directory))
    result = await tool.execute(ToolContext(tmp_path), {})
    payload = json.loads(result.content)
    assert result.is_error and payload["status"] == "executable_unavailable"
    assert payload["executable"] == executable
    assert "separate argv entries" in payload["next_step"]


async def test_missing_script_is_a_launched_interpreter_failure(tmp_path):
    tool = await create_tool(tmp_path, command=[sys.executable, "{artifact_dir}/absent.py"])
    result = await tool.execute(ToolContext(tmp_path), {})
    assert result.is_error
    assert "absent.py" in result.content and "exit code: 2" in result.content
    assert "executable_unavailable" not in result.content


@pytest.mark.parametrize(
    "error,status",
    [
        (PermissionError(errno.EACCES, "denied"), "launch_permission_denied"),
        (OSError(errno.ENOEXEC, "exec format"), "launch_failed"),
    ],
)
async def test_other_launch_errors_are_structured(tmp_path, monkeypatch, error, status):
    tool = await create_tool(tmp_path)
    monkeypatch.setattr("harness.tools.artifacts.run_process", AsyncMock(side_effect=error))
    result = await tool.execute(ToolContext(tmp_path), {})
    assert result.is_error
    assert json.loads(result.content)["status"] == status


async def test_script_failure_retains_stderr_and_exit_code_without_retry(tmp_path):
    marker = tmp_path / "runs"
    tool = await create_tool(
        tmp_path,
        files={
            "main.py": (
                f"from pathlib import Path\np=Path({str(marker)!r})\n"
                'p.write_text(p.read_text()+"x" if p.exists() else "x")\nsys.stdin\n'
            )
        },
    )
    result = await tool.execute(ToolContext(tmp_path), {})
    assert result.is_error
    assert "NameError" in result.content and "sys" in result.content
    assert "exit code: 1" in result.content
    assert marker.read_text() == "x"
