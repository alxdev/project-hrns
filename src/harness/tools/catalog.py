from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from .base import Tool
from .loader import load_artifact_tool


class ToolCatalog(Mapping[str, Tool]):
    """Installed tools plus user artifacts, discovered without granting access.

    Artifact lookup is lazy so a running agent can request a tool another
    subagent just created. Persisted profile names resolve through this same
    catalog on the next startup.
    """

    def __init__(self, tools: Iterable[Tool], artifact_root: Path) -> None:
        self._native = {tool.definition.name: tool for tool in tools}
        self.artifact_root = artifact_root.resolve()

    def __getitem__(self, name: str) -> Tool:
        if name in self._native:
            return self._native[name]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise KeyError(name)
        if not (self.artifact_root / name / "manifest.json").is_file():
            raise KeyError(name)
        return load_artifact_tool(self.artifact_root, name)

    def __iter__(self) -> Iterator[str]:
        names = set(self._native)
        if self.artifact_root.is_dir():
            names.update(
                path.name
                for path in self.artifact_root.iterdir()
                if path.is_dir()
                and re.fullmatch(r"[a-z][a-z0-9_]*", path.name)
                and (path / "manifest.json").is_file()
            )
        return iter(sorted(names))

    def __len__(self) -> int:
        return sum(1 for _ in self)
