import asyncio
from pathlib import Path
from types import SimpleNamespace

from harness.agent.loop import MCPDiscoveryEvent
from harness.api.types import ToolDefinition
from harness.commands import CommandContext, build_default_commands
from harness.mcp import MCPConfigStore, MCPServerConfig, MCPToolManager
from harness.provider import MockProvider
from harness.subagents import SubagentManager, build_default_profiles
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

    assert await manager.tools_for("coder") == ()
    assert await manager.tools_for("coder") == ()
    assert attempts == 1
    assert [event.phase for event in events.events] == ["started", "failed"]


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
