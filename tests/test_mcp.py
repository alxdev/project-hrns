import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.agent.loop import MCPDiscoveryEvent
from harness.api.types import ToolDefinition
from harness.commands import CommandContext, build_default_commands
from harness.mcp import MCPConfigStore, MCPServerConfig, MCPToolManager
from harness.mcp.auth import MCPAuthState
from harness.mcp.config import resolve_auth_headers
from harness.mcp.oauth_compat import install_root_slash_issuer_compatibility
from harness.mcp.tools import MCPTool
from harness.provider import MockProvider
from harness.subagents import SubagentManager, build_default_profiles
from harness.subagents.tools import DelegationTool
from harness.tools.base import ToolContext
from harness.tools.read_file import ReadFileTool
from harness.tools.registry import ToolRegistry
from harness.transcript import Transcript


class EventCollector:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class FakeClient:
    def __init__(self, _url):
        self.closed = False
        self.calls = []
        self.owner_task = None

    async def __aenter__(self):
        self.owner_task = asyncio.current_task()
        return self

    async def __aexit__(self, *_args):
        assert asyncio.current_task() is self.owner_task
        self.closed = True

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="search",
                    description="Search documents.",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                )
            ]
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            content=[SimpleNamespace(text="found it")], structured_content=None, is_error=False
        )


def test_config_store_round_trip_and_scope(tmp_path: Path):
    store = MCPConfigStore(tmp_path / "mcp.json")
    store.add(MCPServerConfig("docs", "https://example.com/mcp", "agent", "explorer"))
    store.add(MCPServerConfig("mail", "https://mail.example.com/mcp", "global"))

    assert store.load() == [
        MCPServerConfig("docs", "https://example.com/mcp", "agent", "explorer"),
        MCPServerConfig("mail", "https://mail.example.com/mcp", "global"),
    ]
    assert store.remove("docs") == 1


def test_mcp_auth_config_stores_env_var_names_not_secret_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_MCP_TOKEN", "secret-value")
    store = MCPConfigStore(tmp_path / "mcp.json")
    auth = {"type": "bearer", "token_env": "TEST_MCP_TOKEN"}
    store.add(MCPServerConfig("docs", "https://example.com/mcp", "global", auth=auth))

    assert store.load()[0].auth == auth
    assert "secret-value" not in store.path.read_text(encoding="utf-8")
    assert resolve_auth_headers(auth) == {"Authorization": "Bearer secret-value"}


def test_mcp_custom_headers_resolve_environment_values(monkeypatch):
    monkeypatch.setenv("TEST_MCP_KEY", "secret-value")

    assert resolve_auth_headers({"type": "headers", "headers": {"X-API-Key": "TEST_MCP_KEY"}}) == {
        "X-API-Key": "secret-value"
    }


async def test_mcp_auth_secret_is_prompted_once_and_remains_session_only(monkeypatch):
    monkeypatch.delenv("SESSION_MCP_SECRET", raising=False)
    prompts = []

    async def prompt(variable):
        prompts.append(variable)
        return "private-value"

    state = MCPAuthState(prompt_secret=prompt)

    assert await state.secret("SESSION_MCP_SECRET") == "private-value"
    assert await state.secret("SESSION_MCP_SECRET") == "private-value"
    assert prompts == ["SESSION_MCP_SECRET"]
    assert "SESSION_MCP_SECRET" not in os.environ
    assert state.secrets == {"SESSION_MCP_SECRET": "private-value"}


def test_oauth_issuer_compatibility_only_accepts_root_trailing_slash():
    from mcp.client.auth import oauth2

    install_root_slash_issuer_compatibility()
    metadata = SimpleNamespace(issuer="https://accounts.google.com")

    oauth2.validate_metadata_issuer(metadata, "https://accounts.google.com/")
    with pytest.raises(Exception, match="issuer mismatch"):
        oauth2.validate_metadata_issuer(metadata, "https://accounts.example.com/")
    with pytest.raises(Exception, match="issuer mismatch"):
        oauth2.validate_metadata_issuer(metadata, "https://accounts.google.com/oauth")


async def test_mcp_tool_reports_leaf_error_from_oauth_exception_group():
    class FailedClient:
        async def __aenter__(self):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [ValueError("Authorization server metadata issuer mismatch")],
            )

        async def __aexit__(self, *_args):
            return False

    tool = MCPTool(
        lambda _url: FailedClient(),
        "https://gmailmcp.googleapis.com/mcp/v1",
        "gmail",
        "search_threads",
        ToolDefinition("mcp_gmail_search_threads", "Search", {"type": "object"}),
    )

    result = await tool.execute(ToolContext(Path.cwd()), {})

    assert result.is_error
    assert "Authorization server metadata issuer mismatch" in result.content
    assert "unhandled errors in a TaskGroup" not in result.content


def test_mcp_auth_config_requires_environment_variable_name(tmp_path: Path):
    store = MCPConfigStore(tmp_path / "mcp.json")

    try:
        store.add(
            MCPServerConfig("docs", "https://example.com/mcp", "global", auth={"type": "bearer"})
        )
    except ValueError as exc:
        assert "token_env" in str(exc)
    else:
        raise AssertionError("invalid auth configuration should be rejected")


def test_mcp_command_persists_profile_or_global_registration(tmp_path: Path):
    class Subagents:
        profiles = {"explorer": object()}

    class MCPStub:
        def __init__(self):
            self.registrations = []

        def register(self, server):
            self.registrations.append(server)

        def describe(self):
            return "none"

    mcp = MCPStub()
    context = CommandContext(
        agent=object(),
        provider=MockProvider(),
        tools=ToolRegistry(),
        subagents=Subagents(),
        mcp=mcp,
    )
    commands = build_default_commands()

    result = commands.dispatch(
        "/mcp add https://docs.example.com/mcp --agent explorer --name docs", context
    )

    assert result is not None and "registered docs" in result.text
    assert mcp.registrations == [
        MCPServerConfig("docs", "https://docs.example.com/mcp", "agent", "explorer")
    ]


def test_mcp_command_accepts_provider_neutral_oauth_configuration():
    class Subagents:
        profiles = {"mail": object()}

    class MCPStub:
        def __init__(self):
            self.registrations = []

        def register(self, server):
            self.registrations.append(server)

    mcp = MCPStub()
    context = CommandContext(
        agent=object(),
        provider=MockProvider(),
        tools=ToolRegistry(),
        subagents=Subagents(),
        mcp=mcp,
    )

    result = build_default_commands().dispatch(
        "/mcp add https://mail.example.com/mcp --agent mail --name mail "
        "--auth oauth --client-id-env MAIL_CLIENT_ID "
        "--client-secret-env MAIL_CLIENT_SECRET "
        "--redirect-uri http://127.0.0.1:8765/callback --scopes 'mail.read mail.write'",
        context,
    )

    assert result is not None and "registered mail" in result.text
    assert mcp.registrations[0].auth == {
        "type": "oauth",
        "client_id_env": "MAIL_CLIENT_ID",
        "client_secret_env": "MAIL_CLIENT_SECRET",
        "redirect_uri": "http://127.0.0.1:8765/callback",
        "scopes": ["mail.read", "mail.write"],
    }


async def test_mcp_discovery_is_scoped_cached_and_connections_are_task_local(tmp_path: Path):
    store = MCPConfigStore(tmp_path / "mcp.json")
    store.add(MCPServerConfig("docs", "https://docs.example.com/mcp", "agent", "explorer"))
    store.add(MCPServerConfig("mail", "https://mail.example.com/mcp", "global"))
    events = EventCollector()
    clients = []

    def client_factory(url):
        client = FakeClient(url)
        clients.append(client)
        return client

    manager = MCPToolManager(store, events)
    manager._client_factory = client_factory

    explorer_tools = await manager.tools_for("explorer")
    coder_tools = await manager.tools_for("coder")
    again = await manager.tools_for("explorer")

    assert [tool.definition.name for tool in explorer_tools] == [
        "mcp_docs_search",
        "mcp_mail_search",
    ]
    assert [tool.definition.name for tool in coder_tools] == ["mcp_mail_search"]
    assert again is explorer_tools
    assert len(clients) == 2
    assert all(client.closed for client in clients)
    result = await explorer_tools[0].execute(ToolContext(tmp_path), {"query": "hello"})
    assert result.content == "found it"
    assert clients[2].calls == [("search", {"query": "hello"})]
    assert all(client.closed for client in clients)


async def test_mcp_discovery_failure_is_cached_and_reported(tmp_path: Path):
    store = MCPConfigStore(tmp_path / "mcp.json")
    store.add(MCPServerConfig("mail", "https://mail.example.com/mcp", "agent", "coder"))
    events = EventCollector()
    attempts = 0

    class FailedClient(FakeClient):
        async def list_tools(self):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("connection timed out")

    manager = MCPToolManager(store, events)
    manager._client_factory = FailedClient

    with pytest.raises(RuntimeError, match="connection timed out"):
        await manager.tools_for("coder")
    with pytest.raises(RuntimeError, match="connection timed out"):
        await manager.tools_for("coder")
    assert attempts == 1
    assert [event.phase for event in events.events] == ["started", "failed"]


async def test_missing_oauth_environment_fails_gmail_delegation_before_model_call(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    store = MCPConfigStore(tmp_path / "mcp.json")
    store.add(
        MCPServerConfig(
            "gmail",
            "https://gmail.example.com/mcp",
            "agent",
            "gmail",
            auth={
                "type": "oauth",
                "client_id_env": "GOOGLE_CLIENT_ID",
                "client_secret_env": "GOOGLE_CLIENT_SECRET",
                "redirect_uri": "http://127.0.0.1:8765/callback",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            },
        )
    )
    events = EventCollector()
    mcp = MCPToolManager(store, events)
    agent_calls = []

    def factory(_profile):
        agent_calls.append("factory")
        raise AssertionError("Gmail agent must not be constructed without its tools")

    subagents = SubagentManager(
        factory,
        build_default_profiles([]),
        tool_loader=mcp.tools_for,
        events=events,
    )
    tool = DelegationTool("gmail", "Use Gmail tools")
    tool.bind(subagents)
    result = await tool.execute(ToolContext(tmp_path), {"task": "Read my latest emails"})

    assert result.is_error
    assert "GOOGLE_CLIENT_ID is not set" in result.content
    assert agent_calls == []
    assert subagents.records()[0].status.value == "failed"
    assert [event.phase for event in events.events if isinstance(event, MCPDiscoveryEvent)] == [
        "started",
        "failed",
    ]


def test_transcript_reports_mcp_discovery_activity_and_outcome():
    transcript = Transcript()
    started, _ = transcript.apply(MCPDiscoveryEvent("explorer", "Google Gmail", "started"))
    assert transcript.activity == "explorer is discovering Google Gmail MCP tools..."
    assert started is not None and started.status == "running"

    completed, is_new = transcript.apply(
        MCPDiscoveryEvent("explorer", "Google Gmail", "completed", tool_count=4)
    )
    assert is_new is False
    assert completed is not None
    assert completed.content == "Google Gmail MCP tools loaded (4 tools) for explorer"


async def test_subagent_discovers_tools_before_agent_factory():
    profile = build_default_profiles([])["explorer"]
    order = []

    class AgentStub:
        async def send(self, _prompt):
            return "done"

    async def load_tools(name):
        order.append(f"discover:{name}")
        return [FakeTool()]

    def factory(effective_profile):
        order.append("factory")
        assert "mcp_docs_search" in [tool.definition.name for tool in effective_profile.tools]
        return AgentStub()

    manager = SubagentManager(factory, {"explorer": profile}, tool_loader=load_tools)
    await manager.delegate("explorer", "research")

    assert order == ["discover:explorer", "factory"]


def test_tools_and_agents_commands_show_scoped_mcp_tools_and_descriptions():
    profiles = build_default_profiles([ReadFileTool()])
    subagents = SubagentManager(lambda _profile: None, profiles)

    class MCPView:
        def cached_tools_for(self, profile):
            return (FakeTool(),) if profile == "explorer" else ()

        def discovery_status(self, profile):
            return [("docs", "loaded (1 tools)")] if profile == "explorer" else []

    context = CommandContext(
        agent=object(),
        provider=MockProvider(),
        tools=ToolRegistry(),
        subagents=subagents,
        mcp=MCPView(),
    )
    commands = build_default_commands()
    tools_result = commands.dispatch("/tools", context)
    agents_result = commands.dispatch("/agents", context)

    assert tools_result is not None
    assert "mcp_docs_search — Search" in tools_result.text
    assert "MCP docs: loaded (1 tools)" in tools_result.text
    read_file_index = tools_result.text.index("read_file —")
    mcp_tool_index = tools_result.text.index("mcp_docs_search —")
    assert "\n\n" in tools_result.text[read_file_index:mcp_tool_index]
    assert agents_result is not None
    assert "agent_root — Coordinates requests" in agents_result.text
    assert "agent_explorer — Investigate the codebase" in agents_result.text
    assert "tools: read_file, mcp_docs_search" in agents_result.text
    assert "agent_root — Coordinates" in agents_result.text
    assert "\n\n  agent_explorer —" in agents_result.text


class FakeTool:
    @property
    def definition(self):
        return ToolDefinition("mcp_docs_search", "Search", {"type": "object"})

    async def execute(self, _context, _arguments):
        raise NotImplementedError
