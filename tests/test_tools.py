from harness.tools.read_file import ReadFileTool
from harness.tools.registry import ToolRegistry


def test_registry_definitions_are_sorted():
    registry = ToolRegistry([ReadFileTool()])
    assert [item.name for item in registry.definitions()] == ["read_file"]


def test_duplicate_tools_are_rejected():
    tool = ReadFileTool()
    try:
        ToolRegistry([tool, tool])
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate tool should fail")

