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
            description=(
                "Investigate the codebase and return a focused answer. "
                "Prefer this over reading files yourself for read-only questions."
            ),
            tools=read_only,
            system_prompt=(
                "You are an exploration subagent. Investigate the codebase using your "
                "read-only tools and return a concise, evidence-based answer."
            ),
            permission_mode="inherit",
        ),
        "coder": SubagentProfile(
            name="coder",
            description="Implement a focused coding task using the available workspace tools.",
            tools=coding,
            system_prompt=(
                "You are a coding subagent. Make only the changes needed for the assigned "
                "task, then summarize what you changed and verified."
            ),
            permission_mode="inherit",
        ),
    }
