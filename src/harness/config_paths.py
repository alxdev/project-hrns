"""Resolve workspace and user-owned Project HRNS configuration files."""

from __future__ import annotations

from pathlib import Path


def user_config_directory(harness_name: str = "project-hrns") -> Path:
    """Return the per-user configuration and data directory."""
    return Path.home() / f".{harness_name}"


def resolve_config_path(filename: str, workspace: Path, user_directory: Path) -> Path:
    """Prefer a workspace config file, then the corresponding user config file.

    When neither exists, return the user path so optional writers such as MCP
    registration persist under the user's configuration directory.
    """
    project_path = workspace / filename
    if project_path.is_file():
        return project_path
    return user_directory / filename


def resolve_instruction_path(workspace: Path, user_directory: Path) -> Path | None:
    """Choose project AGENTS.md over user AGENTS.md; never combine the files."""
    project_path = workspace / "AGENTS.md"
    if project_path.is_file():
        return project_path
    user_path = user_directory / "AGENTS.md"
    if user_path.is_file():
        return user_path
    return None
