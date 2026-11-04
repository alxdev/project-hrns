"""Bounded, workspace-isolated JSON memory with atomic POSIX transactions.

Records are JSON objects of at most 512 KiB, with UUID IDs and a workspace key.
The envelope is limited to 16 MiB and 10,000 records. JSON values have at most
32 nesting levels and 100,000 nodes per record; integers fit in 256 bits.
These limits are storage guards, not a substitute for the memory record schema.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported runtime is POSIX
    fcntl = None  # type: ignore[assignment]

MAX_RECORD_BYTES = 512 * 1024
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 100_000


class MemoryStoreError(ValueError):
    """A memory operation could not safely read or update persisted data."""


class MemoryStoreLockError(MemoryStoreError):
    """Cross-process locking is unsupported or could not be acquired in time."""


def _record_id(value: Any) -> UUID:
    if not isinstance(value, str) or len(value) > 36:
        raise MemoryStoreError("Memory record id must be a UUID string")
    try:
        return UUID(value)
    except ValueError as exc:
        raise MemoryStoreError("Memory record id must be a UUID string") from exc


def _check_json(value: Any) -> None:
    """Reject non-JSON objects and excessive shapes before serializing them."""
    pending = [(value, 0)]
    count = 0
    text_size = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise MemoryStoreError("Memory record exceeds JSON complexity limits")
        if item is None or type(item) is bool:
            continue
        if type(item) is str:
            text_size += len(item)
            if text_size > MAX_RECORD_BYTES:
                raise MemoryStoreError("Memory record text exceeds size limit")
        elif type(item) is int:
            if item.bit_length() > 256:
                raise MemoryStoreError("Memory record integer exceeds 256 bits")
        elif type(item) is float:
            if not math.isfinite(item):
                raise MemoryStoreError("Memory record numbers must be finite")
        elif type(item) is list:
            if len(item) + len(pending) + count > MAX_JSON_NODES:
                raise MemoryStoreError("Memory record exceeds JSON complexity limits")
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is dict:
            if len(item) * 2 + len(pending) + count > MAX_JSON_NODES:
                raise MemoryStoreError("Memory record exceeds JSON complexity limits")
            for key, child in item.items():
                if type(key) is not str:
                    raise MemoryStoreError("Memory record object keys must be strings")
                pending.extend(((key, depth + 1), (child, depth + 1)))
        else:
            raise MemoryStoreError("Memory record contains a non-JSON value")


def _encode(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise MemoryStoreError("Memory data cannot be encoded as JSON") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MemoryStoreError("Memory JSON contains duplicate object keys")
        result[key] = value
    return result


class MemoryStore:
    """Persist records under a canonical workspace hash, without lost updates."""

    def __init__(self, data_directory: Path, workspace: Path, lock_timeout: float = 2.0) -> None:
        if not math.isfinite(lock_timeout) or lock_timeout < 0:
            raise MemoryStoreError("Memory lock timeout must be finite and nonnegative")
        self.workspace = workspace.resolve()
        self.workspace_key = hashlib.sha256(str(self.workspace).encode()).hexdigest()
        self.path = data_directory / "memory" / self.workspace_key / "records.json"
        self.lock_timeout = lock_timeout

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:
            raise MemoryStoreLockError("Memory storage requires POSIX fcntl locking")
        try:
            descriptor = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise MemoryStoreError(f"Cannot open memory lock: {exc}") from exc
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MemoryStoreLockError("Timed out waiting for memory lock") from exc
                    time.sleep(min(0.01, remaining))
            yield
        except OSError as exc:
            raise MemoryStoreError(f"Memory storage operation failed: {exc}") from exc
        finally:
            os.close(descriptor)

    def _validate_record(self, record: Any) -> UUID:
        if type(record) is not dict:
            raise MemoryStoreError("Memory records must be JSON objects")
        identity = _record_id(record.get("id"))
        if record.get("workspace_key") != self.workspace_key:
            raise MemoryStoreError("Memory record belongs to a different workspace")
        _check_json(record)
        if len(_encode(record)) > MAX_RECORD_BYTES:
            raise MemoryStoreError("Memory record exceeds size limit")
        return identity

    def _read(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            return []
        if len(raw) > MAX_STORE_BYTES:
            raise MemoryStoreError("Memory store exceeds size limit")
        try:
            envelope = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise MemoryStoreError(f"Invalid memory JSON: {exc}") from exc
        if (
            type(envelope) is not dict
            or type(envelope.get("version")) is not int
            or envelope.get("version") != 1
            or envelope.get("workspace_key") != self.workspace_key
            or envelope.get("workspace") != str(self.workspace)
            or type(envelope.get("records")) is not list
            or set(envelope) != {"version", "workspace_key", "workspace", "records"}
        ):
            raise MemoryStoreError("Invalid memory envelope or workspace identity")
        records: list[dict[str, Any]] = envelope["records"]
        if len(records) > MAX_RECORDS:
            raise MemoryStoreError("Memory store exceeds record count limit")
        seen: set[UUID] = set()
        for record in records:
            identity = self._validate_record(record)
            if identity in seen:
                raise MemoryStoreError("Memory store contains duplicate record IDs")
            seen.add(identity)
        return records

    def load(self) -> list[dict[str, Any]]:
        """Read validated records; an absent store creates no directories or files."""
        if not self._exists():
            return []
        with self._locked():
            return self._read()

    def _exists(self) -> bool:
        # Path.exists() can suppress permission and filesystem errors on Python 3.14.
        try:
            self.path.stat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise MemoryStoreError(f"Cannot inspect memory store: {exc}") from exc
        return True

    def _write(self, records: list[dict[str, Any]]) -> None:
        payload = _encode(
            {
                "version": 1,
                "workspace_key": self.workspace_key,
                "workspace": str(self.workspace),
                "records": records,
            }
        )
        if len(payload) > MAX_STORE_BYTES:
            raise MemoryStoreError("Memory store exceeds size limit")
        descriptor, name = tempfile.mkstemp(prefix=".records-", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def append(self, record: dict[str, Any], max_records: int) -> None:
        """Atomically append a unique record, retaining the newest bounded count."""
        if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS:
            raise MemoryStoreError(f"Memory max_records must be between 1 and {MAX_RECORDS}")
        identity = self._validate_record(record)
        # Snapshot caller-owned containers before entering the storage transaction.
        snapshot = json.loads(_encode(record))
        if fcntl is None:
            raise MemoryStoreLockError("Memory storage requires POSIX fcntl locking")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise MemoryStoreError(f"Cannot create memory directory: {exc}") from exc
        with self._locked():
            records = self._read()
            if any(_record_id(item["id"]) == identity for item in records):
                raise MemoryStoreError("Memory record ID already exists")
            self._write((records + [snapshot])[-max_records:])

    def forget(self, record_id: str) -> bool:
        """Atomically remove one record; missing records leave the store untouched."""
        identity = _record_id(record_id)
        if not self._exists():
            return False
        with self._locked():
            records = self._read()
            retained = [item for item in records if _record_id(item["id"]) != identity]
            if len(retained) == len(records):
                return False
            self._write(retained)
            return True
