"""Host-rendered focused task handoffs and runtime notices."""


def render_handoff(task: str, notices: list[str]) -> str:
    sections = ["Focused assigned task:\n" + task]
    if notices:
        sections.append("Host notices:\n" + "\n".join(notices))
    return "\n\n".join(sections)
