"""Typed failure for an existing artifact that cannot be safely loaded."""

from pathlib import Path


class InvalidArtifactError(ValueError):
    def __init__(self, name: str, path: Path, reason: str) -> None:
        self.name = name
        self.path = path
        self.reason = reason
        super().__init__(f"invalid tool artifact {name!r} at {path}: {reason}")
