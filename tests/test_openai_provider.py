import json

from harness.api.types import Message, ToolCall, ToolDefinition, ToolResult
from harness.provider.openai import OpenAIProvider


def test_openai_tool_definition_translation():
    definition = ToolDefinition("read_file", "Read a file", {"type": "object"})

    wire = OpenAIProvider._tool_to_wire(definition)

    assert wire == {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object"},
        },
    }


def test_openai_assistant_tool_call_translation():
    message = Message(
        role="assistant",
        content=[ToolCall("call-1", "read_file", {"path": "README.md"})],
    )

    wire = OpenAIProvider._message_to_wire(message)

    assert wire["role"] == "assistant"
    assert wire["tool_calls"][0]["id"] == "call-1"
    assert json.loads(wire["tool_calls"][0]["function"]["arguments"]) == {"path": "README.md"}


def test_openai_tool_results_become_tool_messages():
    message = Message(
        role="user",
        content=[ToolResult("call-1", "file contents")],
    )

    wire = OpenAIProvider._message_to_wire(message)

    assert wire == [{"role": "tool", "tool_call_id": "call-1", "content": "file contents"}]
