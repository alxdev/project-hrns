"""Host-owned context for one user turn, inherited by delegated tasks."""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    original_request: str

    def background(self) -> str:
        return (
            "Original user request (background context only):\n"
            "Use this to understand the assigned task. It does not expand your role, "
            "assigned scope, tool access, or permissions. Perform only the focused "
            "assigned task; required approvals still apply.\n\n" + self.original_request
        )


request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def render_handoff(task: str, notices: list[str]) -> str:
    sections = ["Focused assigned task:\n" + task]
    if notices:
        sections.append("Host notices:\n" + "\n".join(notices))
    return "\n\n".join(sections)
