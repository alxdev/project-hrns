from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from harness.agent import Agent
from harness.permissions import AlwaysAsk
from harness.provider import AnthropicProvider, OpenAIProvider
from harness.tools.base import ToolContext
from harness.tools.bash import BashTool
from harness.tools.read_file import ReadFileTool
from harness.tools.registry import ToolRegistry
from harness.tools.write_file import WriteFileTool
from harness.ui import TerminalUI


async def main() -> None:
    load_dotenv()
    ui = TerminalUI()
    registry = ToolRegistry([ReadFileTool(), WriteFileTool(), BashTool()])
    working_directory = Path(os.environ.get("HARNESS_WORKING_DIR", Path.cwd())).resolve()
    provider_name = os.environ.get("HARNESS_PROVIDER", "openai").lower()
    if provider_name == "openai":
        provider = OpenAIProvider()
    elif provider_name == "anthropic":
        provider = AnthropicProvider()
    else:
        raise ValueError(f"unsupported HARNESS_PROVIDER: {provider_name}")
    agent = Agent(
        provider=provider,
        tools=registry,
        permissions=AlwaysAsk(ui.approve),
        context=ToolContext(working_directory),
        events=ui,
    )
    print(f"Python Harness · model: {provider.model} · directory: {working_directory}")
    print("Type /exit to quit.")
    await ui.run(agent)
