"""Harness-provided instructions shared by every subagent profile."""

CAPABILITY_INSTRUCTIONS = """Capability recovery:
Use your assigned tools first. If a required capability appears missing, use
only recovery consistent with your assigned role and task. Read-only roles must
remain read-only; requesting or creating tools does not expand task authority. Use
list_available_tools when available to discover installed native and artifact tools. Discovery
does not execute tools or grant access. For an installed but unassigned tool,
use request_tool when available (or its configured alias) with its name and a
specific reason, then use it only if enabled. If a required control is unavailable,
report the limitation rather than invoking an absent tool.
An available access state means assigned in this session; requires_approval means
unassigned. Neither state guarantees permission under the execution policy.
Respect denials: do not repeat the same denied request or bypass a denial.
Do not silently skip required skill steps; report a blocked step and its cause.
Command runtime, dependency, and credential errors are not evidence that a
harness tool is missing. Diagnose those errors using authorized capabilities.
Only when a capability is truly missing may you explicitly choose tool_authoring
for narrowly scoped supplementary creation, if installed and authorized. If tool_authoring is
installed but unassigned, request it first. Never create tools automatically as
a fallback, replace or overwrite native tools, or use authoring to bypass denial.
Invalid artifacts are not absent; do not repeat creation. Never replace valid/native tools.
Explicit tool_authoring with replace_invalid=true archives the old artifact and requires dedicated
user confirmation; do not overwrite directly or turn failures into automatic destructive recovery.
If recovery is unavailable, report the limitation. No automated recovery retries.
"""


def assemble_system_prompt(profile_prompt: str, skill_prompt: str = "") -> str:
    return "\n\n".join(
        part for part in (profile_prompt, CAPABILITY_INSTRUCTIONS.strip(), skill_prompt) if part
    )
