import asyncio
import json
import stat
from pathlib import Path

import pytest

from harness.tools.access import persist_profile_tool_access
from harness.tools.loader import load_tools


@pytest.fixture
def catalog():
    return {
        tool.definition.name: tool for tool in load_tools(Path(__file__).parents[1] / "tools.json")
    }


def write_profiles(tmp_path):
    path = tmp_path / "subagents.json"
    path.write_text(json.dumps({"profiles": {"coder": {"tools": []}}, "setting": "original"}))
    path.chmod(0o600)
    return path


async def test_saving_access_preserves_edits_made_during_approval(tmp_path, catalog):
    path = write_profiles(tmp_path)

    async def approve(_definition, _arguments):
        latest = json.loads(path.read_text())
        latest["setting"] = "edited while approval was open"
        latest["profiles"]["coder"]["tools"].append("read_file")
        latest["profiles"]["coder"]["tool_artifacts"] = ["external_helper"]
        path.write_text(json.dumps(latest))
        return True

    assert await persist_profile_tool_access(path, "coder", "bash", approve, catalog)

    result = json.loads(path.read_text())
    assert result["setting"] == "edited while approval was open"
    assert result["profiles"]["coder"]["tools"] == ["read_file", "bash"]
    assert result["profiles"]["coder"]["tool_artifacts"] == ["external_helper"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


async def test_simultaneous_approved_updates_both_survive(tmp_path, catalog):
    path = write_profiles(tmp_path)
    approvals = asyncio.Barrier(2)

    async def approve(_definition, _arguments):
        await approvals.wait()
        return True

    results = await asyncio.gather(
        persist_profile_tool_access(path, "coder", "bash", approve, catalog),
        persist_profile_tool_access(path, "coder", "read_file", approve, catalog),
    )

    assert results == [True, True]
    assert set(json.loads(path.read_text())["profiles"]["coder"]["tools"]) == {"bash", "read_file"}


async def test_atomic_save_failure_preserves_original_file(tmp_path, monkeypatch, catalog):
    path = write_profiles(tmp_path)
    original = path.read_bytes()

    async def approve(_definition, _arguments):
        return True

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("harness.tools.access.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        await persist_profile_tool_access(path, "coder", "bash", approve, catalog)

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


async def test_profile_removed_during_approval_is_not_recreated(tmp_path, catalog):
    path = write_profiles(tmp_path)

    async def approve(_definition, _arguments):
        path.write_text('{"profiles": {}}')
        return True

    assert not await persist_profile_tool_access(path, "coder", "bash", approve, catalog)
    assert json.loads(path.read_text()) == {"profiles": {}}
