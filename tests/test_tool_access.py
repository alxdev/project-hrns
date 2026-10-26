import asyncio
import json
import stat

import pytest

from harness.tools.access import persist_profile_tool_access


def write_profiles(tmp_path):
    path = tmp_path / "subagents.json"
    path.write_text(json.dumps({"profiles": {"coder": {"tools": []}}, "setting": "original"}))
    path.chmod(0o600)
    return path


async def test_saving_access_preserves_edits_made_during_approval(tmp_path):
    path = write_profiles(tmp_path)

    async def approve(_definition, _arguments):
        latest = json.loads(path.read_text())
        latest["setting"] = "edited while approval was open"
        latest["profiles"]["coder"]["tools"].append("read_file")
        path.write_text(json.dumps(latest))
        return True

    assert await persist_profile_tool_access(path, "coder", "bash", approve)

    result = json.loads(path.read_text())
    assert result["setting"] == "edited while approval was open"
    assert result["profiles"]["coder"]["tools"] == ["read_file", "bash"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


async def test_simultaneous_approved_updates_both_survive(tmp_path):
    path = write_profiles(tmp_path)
    approvals = asyncio.Barrier(2)

    async def approve(_definition, _arguments):
        await approvals.wait()
        return True

    results = await asyncio.gather(
        persist_profile_tool_access(path, "coder", "bash", approve),
        persist_profile_tool_access(path, "coder", "read_file", approve),
    )

    assert results == [True, True]
    assert set(json.loads(path.read_text())["profiles"]["coder"]["tools"]) == {"bash", "read_file"}


async def test_atomic_save_failure_preserves_original_file(tmp_path, monkeypatch):
    path = write_profiles(tmp_path)
    original = path.read_bytes()

    async def approve(_definition, _arguments):
        return True

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("harness.tools.access.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        await persist_profile_tool_access(path, "coder", "bash", approve)

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


async def test_profile_removed_during_approval_is_not_recreated(tmp_path):
    path = write_profiles(tmp_path)

    async def approve(_definition, _arguments):
        path.write_text('{"profiles": {}}')
        return True

    assert not await persist_profile_tool_access(path, "coder", "bash", approve)
    assert json.loads(path.read_text()) == {"profiles": {}}
