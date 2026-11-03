"""JSON text results for capability controls, preserving the ToolResult protocol."""

import json
from typing import Any

from harness.api.types import ToolResult


def capability_result(
    status: str,
    message: str,
    next_step: str,
    *,
    is_error: bool = False,
    tool_call_id: str = "",
    **details: Any,
) -> ToolResult:
    return ToolResult(
        tool_call_id,
        json.dumps({"status": status, "message": message, "next_step": next_step, **details}),
        is_error,
    )
