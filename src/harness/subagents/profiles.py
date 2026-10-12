from __future__ import annotations

from collections.abc import Iterable

from harness.tools.base import Tool

from .models import SubagentProfile


def build_default_profiles(tools: Iterable[Tool]) -> dict[str, SubagentProfile]:
    catalog = {tool.definition.name: tool for tool in tools}
    read_only = tuple(tool for name, tool in catalog.items() if name == "read_file")
    coding = tuple(catalog.values())
    return {
        "explorer": SubagentProfile(
            name="explorer",
            description="Inspect files and answer questions without modifying the workspace.",
            tools=read_only,
        ),
        "coder": SubagentProfile(
            name="coder",
            description="Inspect and modify files, and run workspace commands.",
            tools=coding,
        ),
    }
