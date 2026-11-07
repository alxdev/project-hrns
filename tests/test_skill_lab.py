"""Offline integration checks for manual lab fixtures, not model behavior."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from harness.skills import load_skills
from harness.subagents.profiles import load_profiles
from harness.subagents.tools import DelegationTool
from harness.tools.base import ToolContext, resolve_read_path
from harness.tools.loader import load_tools

ROOT = Path(__file__).resolve().parents[1]
LAB_SKILLS = ("lab-resource-check", "lab-command-check", "lab-text-metrics")


def test_lab_catalog_and_profile_resolve_without_user_home(tmp_path):
    catalog = load_skills(ROOT / "skills.json", ROOT, tmp_path / "isolated-user")
    # Model the lab's required capabilities without inheriting mutable profile
    # grants or resolving any environment-owned artifacts.
    config = {
        "profiles": {
            "skill-lab": {
                "description": "Run isolated lab fixture checks.",
                "system_prompt": "Use only the assigned lab fixtures and capabilities.",
                "tools": ["read_file", "bash", "tool_authoring", "activate_skill"],
                "tool_artifacts": [],
                "skills": ["hello-workflow", *LAB_SKILLS],
                "permission_mode": "inherit",
                "max_turns": 30,
            }
        }
    }
    profile_path = tmp_path / "subagents.json"
    profile_path.write_text(json.dumps(config), encoding="utf-8")
    profiles = load_profiles(profile_path, load_tools(ROOT / "tools.json"))
    profile = profiles["skill-lab"]
    assert profile.skills == ("hello-workflow", *LAB_SKILLS)
    assert profile.permission_mode == "inherit"
    assert profile.max_turns == 30
    assert {tool.definition.name for tool in profile.tools} == {
        "read_file",
        "bash",
        "tool_authoring",
        "activate_skill",
        "list_available_tools",
        "request_tool",
    }
    assert DelegationTool().definition.name == "delegate_subagent"
    assert {"hello-workflow", "project-summary"} <= catalog.skills.keys()
    for name in LAB_SKILLS:
        skill = catalog.get(name)
        assert skill is not None
        assert skill.root == ROOT / ".agents" / "skills" / name
        content = skill.manifest.read_text(encoding="utf-8")
        metadata = yaml.safe_load(content.split("---", 2)[1])
        assert set(metadata) == {"name", "description"}
        assert metadata["name"] == name
        assert metadata["description"] == skill.description
        assert content.rstrip() in catalog.activate(name, profile.skills)


def test_lab_resources_are_listed_and_match(tmp_path):
    catalog = load_skills(ROOT / "skills.json", ROOT, tmp_path / "isolated-user")
    activation = catalog.activate("lab-resource-check", ("lab-resource-check",))
    assert "data/check.json" in activation
    assert "data/sample.txt" in activation
    root = catalog.get("lab-resource-check").root
    assigned_catalog = catalog.for_names(("lab-resource-check",))
    context = ToolContext(
        working_directory=tmp_path,
        allowed_read_roots=tuple(skill.root for skill in assigned_catalog.skills.values()),
        skill_catalog=assigned_catalog,
        allowed_skills=("lab-resource-check",),
    )
    fixture = json.loads((root / "data/check.json").read_text(encoding="utf-8"))
    assert fixture["token"] == "SKILL_LAB_RESOURCE_V1"
    sample_path = resolve_read_path(context, str(root / "data/sample.txt"))
    assert sample_path == (root / "data/sample.txt").resolve()
    assert sample_path.read_bytes() == fixture["expected_text"].encode()
    activation = catalog.activate("lab-command-check", ("lab-command-check",))
    assert "scripts/check.sh" in activation


@pytest.mark.parametrize(
    ("mode", "code", "stdout", "stderr"),
    [
        ("success", 0, "SKILL_LAB_COMMAND_V1 PASS\n", "SIMULATED success: local validation only\n"),
        (
            "dependency-failure",
            42,
            "",
            "SIMULATED dependency failure: lab_demo_dependency unavailable; no lookup performed\n",
        ),
        (
            "credentials-failure",
            43,
            "",
            "SIMULATED credentials failure: lab_demo_token unavailable; no secrets inspected\n",
        ),
        ("invalid", 64, "", "Usage: check.sh success|dependency-failure|credentials-failure\n"),
    ],
)
def test_bundled_command_modes_are_offline(tmp_path, mode, code, stdout, stderr):
    helper = ROOT / ".agents/skills/lab-command-check/scripts/check.sh"
    result = subprocess.run(
        ["/bin/sh", str(helper), mode],
        cwd=tmp_path,
        env={},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (code, stdout, stderr)
    assert list(tmp_path.iterdir()) == []
