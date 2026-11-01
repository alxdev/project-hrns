"""Child-only skill catalogs and unchanged scripted task handoff/model switching."""

import json
from pathlib import Path

import pytest

from harness.api.types import ProviderResponse, TextBlock, ToolCall
from harness.model_catalog import ModelCatalog, ModelChoice
from harness.orchestration import root_system_prompt
from harness.provider import MockProvider

TOOLS = Path(__file__).parents[1] / "tools.json"


def done(text="Done"):
    return ProviderResponse([TextBlock(text)], "end_turn")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    entries = []
    for name, description in (
        ("shared", "Shared analysis workflow"),
        ("review", "Review workflow"),
        ("orphan", "Unassigned workflow"),
    ):
        root = tmp_path / ".agents" / "skills" / name
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\nPRIVATE BODY {name}\n"
        )
        entries.append({"name": name, "description": description, "location": str(root)})
    (tmp_path / "skills.json").write_text(json.dumps({"skills": entries}))
    (tmp_path / "tools.json").write_text(TOOLS.read_text())
    (tmp_path / "subagents.json").write_text(
        json.dumps(
            {
                "profiles": {
                    name: {
                        "description": name,
                        "system_prompt": f"{name} configured role",
                        "tools": ["read_file"],
                        "skills": skills,
                        "permission_mode": "inherit",
                    }
                    for name, skills in (("alpha", ["shared"]), ("beta", ["shared", "review"]))
                }
            }
        )
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return tmp_path


async def test_noninteractive_skill_catalog_is_child_only_with_unchanged_task_handoff(
    workspace, monkeypatch
):
    import harness.main as main_module

    task = "Use shared skill to report the intended result for α\nwith exact input."
    root = MockProvider(
        [
            ProviderResponse([ToolCall("delegate", "delegate_alpha", {"task": task})], "tool_use"),
            done("Received child result"),
        ]
    )
    root.system = root_system_prompt("Root base")
    child = MockProvider([done("Actual child result")])
    child.system = "Child provider base"
    providers = iter([root, child])
    monkeypatch.setattr(main_module, "build_provider", lambda *_args: next(providers))
    assert (
        await main_module.main(
            ["--working-dir", str(workspace), "--prompt", "Use shared", "--allow-tools"]
        )
        == 0
    )
    assert root.system == root_system_prompt("Root base")
    assert "## Available skills" not in root.system
    assert "Shared analysis workflow" not in root.system
    assert "orphan" not in root.system and "PRIVATE BODY" not in root.system
    assert child.system.startswith("alpha configured role")
    assert (
        "## Available skills" in child.system and "shared: Shared analysis workflow" in child.system
    )
    assert "Review workflow" not in child.system
    assert "PRIVATE BODY" not in child.system
    assert child.calls[0][0][-1].content[0].text == "Focused assigned task:\n" + task
    schema = root.calls[0][1][0].input_schema
    assert schema["required"] == ["task"] and set(schema["properties"]) == {"task"}
    assert "Actual child result" in str(root.calls[1][0])
    assert len(root.calls) == 2 and len(child.calls) == 1


async def test_interactive_model_switch_preserves_root_prompt_and_child_only_skills(
    workspace, monkeypatch
):
    import harness.main as main_module
    import harness.ui.interactive as interactive
    import harness.ui.prompt_toolkit_rich as ui

    initial = ModelChoice("mock/initial", "openai", "initial", "Initial", {})
    changed = ModelChoice("mock/changed", "openai", "changed", "Changed", {})
    catalog = ModelCatalog((initial, changed))
    monkeypatch.setattr(ModelCatalog, "load", lambda _root: catalog)
    startup = MockProvider(model="initial")
    startup.system = root_system_prompt("Root provider base")
    switched = MockProvider(
        [
            ProviderResponse(
                [ToolCall("delegate", "delegate_alpha", {"task": "Use shared; report result"})],
                "tool_use",
            ),
            done(),
        ],
        model="changed",
    )
    switched.system = "New provider default"
    child = MockProvider([done("Child result")], model="changed")
    child.system = "Child provider default"

    async def choose(*_args):
        return startup, initial

    monkeypatch.setattr(main_module, "choose_startup_model", choose)
    providers = iter([switched, child])
    monkeypatch.setattr(main_module, "build_provider_for_choice", lambda _choice: next(providers))

    class Output:
        def write(self, _text):
            pass

        async def emit(self, _event):
            pass

    monkeypatch.setattr(ui, "create_ui", lambda _path: (object(), Output()))

    async def run(_self, agent, _commands, context):
        before = agent.provider.system
        assert before == root_system_prompt("Root provider base")
        assert "## Available skills" not in before
        assert "Shared analysis workflow" not in before
        context.select_model(changed.key)
        assert agent.provider is switched and switched.system == before
        await agent.send("Use shared; report result")
        assert child.system.startswith("alpha configured role")
        assert "## Available skills" in child.system
        assert "shared: Shared analysis workflow" in child.system
        assert "Review workflow" not in child.system
        assert "PRIVATE BODY" not in before and "PRIVATE BODY" not in child.system

    monkeypatch.setattr(interactive.InteractiveRunner, "run", run)
    assert await main_module.main(["--working-dir", str(workspace)]) == 0
