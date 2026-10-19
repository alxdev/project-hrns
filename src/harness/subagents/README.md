# Subagents

The subagent package lets the main agent delegate focused work to isolated
agent instances with curated tool sets. Profiles may opt into reuse when they
need stateful conversations.

## Profiles

The default profiles are:

- `explorer` — read-only inspection through `read_file`
- `coder` — `read_file`, `write_file`, and `bash`
- `gmail` — Gmail MCP tools only; searches/reads mail and can create drafts.
  It cannot directly send mail and treats email content as untrusted input.

Each profile also defines a system prompt, maximum turns, permission mode, and
whether its `Agent` may be reused. Default profiles run with fresh isolated
agent state for every delegation. Reuse is available for explicitly
stateful profiles.

Profiles are defined independently in `profiles.py`. Add a profile by creating
a `SubagentProfile` with the tools it is allowed to use.

## Delegation

The main agent receives one tool per profile, such as `delegate_explorer` or
`delegate_coder`. Each tool accepts:

```json
{
  "task": "Find the configuration files and summarize them."
}
```

Each delegation receives a tracking ID and a completed or failed status.
Subagent lifecycle events are emitted through the generic event sink, allowing
different UIs to show or ignore start, completion, and elapsed-time messages.

`SubagentManager.describe()` reports configured profiles, active work, and the
latest delegation records. The interactive `/agents` command displays them alongside
the root and subagent descriptions and their available tools.

## Tool ownership

The root agent is intentionally restricted to delegation tools. It cannot call
`read_file`, `write_file`, or `bash` directly. This enforces a strict planner /
executor boundary:

- `explorer` may use `read_file` for read-only investigation.
- `coder` may use `read_file`, `write_file`, and `bash` for implementation work.

The `coder` profile inherits the execution permission policy. The root agent's
delegation call is routing and does not require approval; in interactive mode,
the actual file operation or shell command is displayed and must be approved
before it runs. In non-interactive mode, it follows the same `--allow-tools`
setting as the root agent.

Consequently, file changes and shell commands must be performed by the `coder`
subagent, even when the task is initiated interactively by the root agent.

The `gmail` profile inherits the same tool approval policy. Interactive Gmail
actions require approval, and Gmail MCP tools are scoped only to this profile.

## MCP tools

MCP servers can be assigned to one profile or made global with `/mcp add`.
Profile-scoped tools are discovered synchronously before the profile's first
agent loop, then cached for the session. Global tools are discovered before the
root agent's first loop and are also included for subagents. Discovery activity
is emitted through the generic event sink; the UI shows the current server and
logs the resulting tool count or the server's error. MCP tools use the same
registry, permission policy, and cancellation path as built-in tools.
