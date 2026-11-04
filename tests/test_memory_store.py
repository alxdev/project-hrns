"""Persistence contracts for bounded, isolated, atomic experience memory."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from harness.memory import store as store_module
from harness.memory.store import MemoryStore, MemoryStoreError, MemoryStoreLockError


def _record(store: MemoryStore, **extra: Any) -> dict[str, Any]:
    return {"id": uuid4().hex, "workspace_key": store.workspace_key, "evidence": "worked", **extra}


def _writer(data: str, workspace: str, count: int) -> None:
    store = MemoryStore(Path(data), Path(workspace), lock_timeout=5)
    for _ in range(count):
        store.append(_record(store), max_records=100)


def _forgetter(data: str, workspace: str, ids: list[str]) -> None:
    store = MemoryStore(Path(data), Path(workspace), lock_timeout=5)
    for identity in ids:
        assert store.forget(identity)


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "data", tmp_path / "workspace")


def test_absent_reads_create_nothing(store: MemoryStore) -> None:
    assert store.load() == []
    assert not store.forget(uuid4().hex)
    assert not store.path.parent.parent.parent.exists()


def test_restart_canonical_workspace_and_permissions(store: MemoryStore) -> None:
    record = _record(store, evidence={"steps": ["one", "two"]})
    store.append(record, max_records=20)
    record["evidence"]["steps"].append("caller mutation")
    restarted = MemoryStore(store.path.parents[2], store.workspace / ".")
    assert restarted.workspace_key == hashlib.sha256(str(store.workspace).encode()).hexdigest()
    assert restarted.path == store.path
    assert restarted.load()[0]["evidence"] == {"steps": ["one", "two"]}
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.with_suffix(".lock").stat().st_mode & 0o777 == 0o600


def test_workspace_isolation(store: MemoryStore) -> None:
    other = MemoryStore(store.path.parents[2], store.workspace.parent / "other")
    first, second = _record(store), _record(other)
    store.append(first, max_records=10)
    other.append(second, max_records=10)
    assert store.path != other.path
    assert store.load() == [first]
    assert other.load() == [second]
    with pytest.raises(MemoryStoreError, match="different workspace"):
        other.append(first, max_records=10)
    other.path.write_bytes(store.path.read_bytes())
    with pytest.raises(MemoryStoreError, match="workspace identity"):
        other.load()


def test_retention_and_forget_preserve_self_contained_evidence(store: MemoryStore) -> None:
    records = [
        _record(store, evidence={"input": f"task {index}", "result": "ok"}) for index in range(4)
    ]
    for record in records:
        store.append(record, max_records=2)
    assert store.load() == records[-2:]
    assert store.forget(records[-1]["id"])
    before = store.path.read_bytes()
    assert not store.forget(records[0]["id"])
    assert store.path.read_bytes() == before
    assert store.load() == records[-2:-1]


def test_duplicate_ids_rejected_before_retention(store: MemoryStore) -> None:
    record = _record(store)
    store.append(record, max_records=1)
    from uuid import UUID

    duplicate = {**record, "id": str(UUID(record["id"]))}
    before = store.path.read_bytes()
    with pytest.raises(MemoryStoreError, match="already exists"):
        store.append(duplicate, max_records=1)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"{}", b"\xff", b'{"a":1,"a":2}'])
def test_corruption_never_replaced(store: MemoryStore, payload: bytes) -> None:
    store.append(_record(store), max_records=10)
    store.path.write_bytes(payload)
    for operation in (
        store.load,
        lambda: store.append(_record(store), 10),
        lambda: store.forget(uuid4().hex),
    ):
        with pytest.raises(MemoryStoreError):
            operation()
        assert store.path.read_bytes() == payload


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"workspace": "/different"},
        {"workspace_key": "different"},
        {"records": {}},
        {"extra": "unexpected"},
    ],
)
def test_invalid_envelopes(store: MemoryStore, change: dict[str, Any]) -> None:
    store.append(_record(store), max_records=10)
    envelope = json.loads(store.path.read_bytes())
    envelope.update(change)
    store.path.write_text(json.dumps(envelope))
    with pytest.raises(MemoryStoreError):
        store.load()


@pytest.mark.parametrize("bad", [{}, [], {"id": "bad"}, {"id": None}])
def test_invalid_records_rejected_before_filesystem_changes(store: MemoryStore, bad: Any) -> None:
    with pytest.raises(MemoryStoreError):
        store.append(bad, max_records=10)
    assert not store.path.parent.exists()


def test_duplicate_and_invalid_persisted_records_rejected(store: MemoryStore) -> None:
    record = _record(store)
    store.append(record, max_records=10)
    envelope = json.loads(store.path.read_bytes())
    for records in ([record, record], [{**record, "workspace_key": "wrong"}], [42]):
        envelope["records"] = records
        store.path.write_text(json.dumps(envelope))
        with pytest.raises(MemoryStoreError):
            store.load()


@pytest.mark.parametrize(
    "evidence", [object(), {1: "value"}, (1, 2), float("nan"), float("inf"), 1 << 300]
)
def test_non_json_records_rejected(store: MemoryStore, evidence: Any) -> None:
    with pytest.raises(MemoryStoreError):
        store.append(_record(store, evidence=evidence), max_records=10)
    assert not store.path.parent.exists()


def test_depth_cycles_and_record_size_bounded(store: MemoryStore) -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    for evidence in (cycle, "x" * store_module.MAX_RECORD_BYTES, ["x" * 10_000] * 100):
        with pytest.raises(MemoryStoreError):
            store.append(_record(store, evidence=evidence), max_records=10)
    assert not store.path.parent.exists()


def test_read_does_not_hide_filesystem_errors(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: Any) -> None:
        raise PermissionError("simulated access denied")

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(MemoryStoreError, match="simulated access denied"):
        store.load()
    with pytest.raises(MemoryStoreError, match="simulated access denied"):
        store.forget(uuid4().hex)


def test_store_read_and_write_size_bounded(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _record(store)
    store.append(first, max_records=10)
    before = store.path.read_bytes()
    monkeypatch.setattr(store_module, "MAX_STORE_BYTES", len(before) + 1)
    with pytest.raises(MemoryStoreError, match="size limit"):
        store.append(_record(store), max_records=10)
    assert store.path.read_bytes() == before
    store.path.write_bytes(before + b"  ")
    with pytest.raises(MemoryStoreError, match="size limit"):
        store.load()


@pytest.mark.parametrize("max_records", [0, -1, True, 10_001, 1.5])
def test_invalid_retention_rejected(store: MemoryStore, max_records: Any) -> None:
    with pytest.raises(MemoryStoreError):
        store.append(_record(store), max_records=max_records)
    assert not store.path.parent.exists()


def test_lock_contention_is_explicit_and_preserves_file(store: MemoryStore) -> None:
    store.append(_record(store), max_records=10)
    before = store.path.read_bytes()
    store.lock_timeout = 0.02
    with store._locked():
        with pytest.raises(MemoryStoreLockError, match="Timed out"):
            store.append(_record(store), max_records=10)
    assert store.path.read_bytes() == before


def test_unsupported_locking_fails_closed(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "fcntl", None)
    with pytest.raises(MemoryStoreLockError, match="POSIX"):
        store.append(_record(store), max_records=10)
    assert not store.path.parent.exists()


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_atomic_failure_preserves_existing_file(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    record = _record(store)
    store.append(record, max_records=10)
    before = store.path.read_bytes()

    def fail(*args: Any) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, failure, fail)
    with pytest.raises(MemoryStoreError, match="simulated disk failure"):
        store.append(_record(store), max_records=10)
    assert store.path.read_bytes() == before
    with pytest.raises(MemoryStoreError, match="simulated disk failure"):
        store.forget(record["id"])
    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob(".records-*.tmp"))


def test_multiprocess_append_and_forget_do_not_lose_updates(store: MemoryStore) -> None:
    originals = [_record(store) for _ in range(12)]
    for record in originals:
        store.append(record, max_records=100)
    context = multiprocessing.get_context("spawn")
    args = (str(store.path.parents[2]), str(store.workspace))
    processes = [context.Process(target=_writer, args=(*args, 10)) for _ in range(3)]
    processes.append(context.Process(target=_forgetter, args=(*args, [r["id"] for r in originals])))
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
    records = store.load()
    assert len(records) == 30
    assert len({record["id"] for record in records}) == 30
    assert not {record["id"] for record in originals} & {record["id"] for record in records}
