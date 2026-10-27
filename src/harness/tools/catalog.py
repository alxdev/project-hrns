from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from copy import deepcopy
from pathlib import Path

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from harness.api.types import ToolDefinition

from .artifact_errors import InvalidArtifactError
from .artifacts import ArtifactTool
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
        self._artifacts: dict[
            str, tuple[tuple[object, ...], Tool | InvalidArtifactError | KeyError]
        ] = {}
        self._definitions: dict[str, tuple[ToolDefinition, ValueError | None]] = {}

    @property
    def native_tools(self) -> Mapping[str, Tool]:
        """Configured metadata, without artifact discovery or definition validation."""
        return self._native

    def _signature(self, name: str) -> tuple[object, ...]:
        signature: list[object] = []
        for path in (
            self.artifact_root,
            self.artifact_root / name,
            self.artifact_root / name / "manifest.json",
        ):
            try:
                info = path.lstat()
            except (FileNotFoundError, NotADirectoryError):
                signature.append(None)
            else:
                identity = (info.st_dev, info.st_ino, info.st_mode)
                signature.append(
                    (*identity, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    if path.name == "manifest.json"
                    else identity
                )
        return tuple(signature)

    def cache_validated_artifact(self, tool: ArtifactTool) -> None:
        """Cache an already validated, installed artifact without validating again.

        Trusted authoring calls this before refresh/persistence callbacks. Future
        lookups still invalidate the entry when filesystem metadata changes.
        """
        name = tool.definition.name
        if (
            name in self._native
            or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or tool.directory != self.artifact_root / name
        ):
            raise ValueError("validated artifact must belong to this catalog's artifact storage")
        signature = self._signature(name)
        if any(entry is None for entry in signature):
            raise ValueError("validated artifact must be installed before caching")
        self._artifacts[name] = (signature, tool)

    def __getitem__(self, name: str) -> Tool:
        if name in self._native:
            tool = self._native[name]
            cached_definition = self._definitions.get(name)
            if cached_definition is None or cached_definition[0] != tool.definition:
                error = None
                try:
                    validator_for(tool.definition.input_schema).check_schema(
                        tool.definition.input_schema
                    )
                except SchemaError as exc:
                    error = ValueError(f"invalid input_schema for tool {name!r}: {exc.message}")
                self._definitions[name] = (deepcopy(tool.definition), error)
            else:
                error = cached_definition[1]
            if error is not None:
                raise error
            return tool
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise KeyError(name)
        signature = self._signature(name)
        cached = self._artifacts.get(name)
        if cached is None or cached[0] != signature:
            try:
                result: Tool | InvalidArtifactError | KeyError = load_artifact_tool(
                    self.artifact_root, name
                )
            except (InvalidArtifactError, KeyError) as exc:
                result = exc
            self._artifacts[name] = (signature, result)
        else:
            result = cached[1]
        if isinstance(result, (InvalidArtifactError, KeyError)):
            raise result
        return result

    def __iter__(self) -> Iterator[str]:
        names = set(self._native)
        if self.artifact_root.is_dir():
            names.update(
                path.name
                for path in self.artifact_root.iterdir()
                if re.fullmatch(r"[a-z][a-z0-9_]*", path.name)
            )
        return iter(sorted(names))

    def __len__(self) -> int:
        return sum(1 for _ in self)
