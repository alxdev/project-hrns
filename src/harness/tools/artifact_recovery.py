"""Portable preservation helpers; other processes must cooperate with our writes."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from weakref import WeakValueDictionary

_locks: WeakValueDictionary[Path, asyncio.Lock] = WeakValueDictionary()


def artifact_lock(path: Path) -> asyncio.Lock:
    lock = _locks.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _locks[path] = lock
    return lock


def require_real_directory(path: Path) -> None:
    """Reject symlinks in the exact directory path, including its ancestors."""
    for directory in (*reversed(path.parents), path):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"directory must not be a symlink or special file: {directory}")


def archive_destination(path: Path) -> Path:
    archive = path.with_name(f"INVALID_{path.name}")
    suffix = 0
    while archive.exists() or archive.is_symlink():
        suffix += 1
        archive = path.with_name(f"INVALID_{path.name}_{suffix}")
    return archive


def rename_exclusive(source: Path, destination: Path) -> None:
    """Refuse occupied destinations under a cooperative single-writer contract.

    stdlib directory rename has no portable atomic no-replace mode. The check and
    rename are not atomic across processes; POSIX can replace an empty directory
    created between them. Our callers serialize their own operations.
    """
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"artifact destination is occupied: {destination}")
    source.rename(destination)


class OwnedStaging:
    """Best-effort cleanup of known staging files and empty directories only."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.directories = {Path("."): root.stat()}
        self.files: dict[Path, str] = {}

    def mkdir(self, relative: Path) -> None:
        current = Path(".")
        for part in relative.parts:
            current /= part
            if current not in self.directories:
                (self.root / current).mkdir()
                self.directories[current] = (self.root / current).stat()

    def write(self, relative: Path, content: str) -> None:
        self.mkdir(relative.parent)
        with (self.root / relative).open("x", encoding="utf-8") as stream:
            stream.write(content)
        self.files[relative] = content

    def _unchanged_directory(self, relative: Path) -> bool:
        current = Path(".")
        for part in (".", *relative.parts):
            current /= part
            info = (self.root / current).lstat()
            original = self.directories[current]
            if not stat.S_ISDIR(info.st_mode) or not os.path.samestat(info, original):
                return False
        return True

    def cleanup(self) -> bool:
        for relative, content in self.files.items():
            try:
                path = self.root / relative
                if (
                    self._unchanged_directory(relative.parent)
                    and stat.S_ISREG(path.lstat().st_mode)
                    and path.read_text(encoding="utf-8") == content
                ):
                    path.unlink()
            except (OSError, ValueError):
                pass
        for relative in sorted(self.directories, key=lambda p: len(p.parts), reverse=True):
            try:
                if self._unchanged_directory(relative):
                    (self.root / relative).rmdir()
            except (OSError, ValueError):
                pass
        return self.root.exists() or self.root.is_symlink()
