from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness.config_paths import resolve_instruction_path


@dataclass(frozen=True)
class ProjectContext:
    """Root-orchestrator instructions for the current harness working directory."""

    working_directory: Path
    root_path: Path | None = None
    root_content: str = ""

    @property
    def has_root_context(self) -> bool:
        return bool(self.root_content)

    def system_prompt(self, base_prompt: str) -> str:
        """Append shared project instructions to a system prompt."""
        sections = [base_prompt.rstrip()]
        if self.root_content:
            sections.append("# Project context (from AGENTS.md)\n\n" + self.root_content.strip())
        return "\n\n".join(sections)

    def describe(self) -> str:
        lines = [f"project context directory: {self.working_directory}"]
        if self.root_path is None:
            lines.append("  AGENTS.md: not found")
        else:
            lines.append(f"  AGENTS.md: {self.root_path} ({len(self.root_content)} characters)")
        return "\n".join(lines)


def load_project_context(
    working_directory: Path,
    user_directory: Path | None = None,
) -> ProjectContext:
    """Load project AGENTS.md, falling back to the user's instructions file.

    The project file replaces the user file when present; the two are never combined.
    """
    if user_directory is None:
        user_directory = Path.home() / ".project-hrns"
    root_path = resolve_instruction_path(working_directory, user_directory)
    root_content = _read_optional(root_path) if root_path is not None else ""
    return ProjectContext(
        working_directory=working_directory,
        root_path=root_path,
        root_content=root_content,
    )


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
