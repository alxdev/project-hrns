from pathlib import Path

from harness.tools.loader import load_tools
from harness.tools.registry import ToolRegistry

BUILTIN_TOOLS_PATH = Path(__file__).parents[1] / "tools.json"


def test_registry_definitions_are_sorted():
    registry = ToolRegistry(load_tools(BUILTIN_TOOLS_PATH))
    assert [item.name for item in registry.definitions()] == [
        "activate_skill",
        "bash",
        "list_available_tools",
        "read_file",
        "request_tool",
        "tool_authoring",
        "write_file",
    ]


def test_tool_metadata_comes_from_json(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(
        '{"tools": {"renamed": {"handler": "read_file", '
        '"description": "Custom description", "input_schema": '
        '{"type": "object"}}}}',
        encoding="utf-8",
    )

    tool = load_tools(path)[0]

    assert tool.definition.name == "renamed"
    assert tool.definition.description == "Custom description"
    assert tool.definition.input_schema == {"type": "object"}


def test_duplicate_tools_are_rejected():
    tool = load_tools(BUILTIN_TOOLS_PATH)[0]
    try:
        ToolRegistry([tool, tool])
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate tool should fail")
