# Subagents

The subagent package lets the main agent delegate focused work to isolated
agent instances with curated tool sets. Profiles may opt into reuse when they
need stateful conversations.

## Profiles

The default profiles are:

- `explorer` — read-only inspection through `read_file`
- `coder` — `read_file`, `write_file`, and `bash`

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
latest delegation records. The interactive `/subagents` command displays it.

## Tool ownership

The root agent is intentionally restricted to delegation tools. It cannot call
`read_file`, `write_file`, or `bash` directly. This enforces a strict planner /
executor boundary:

- `explorer` may use `read_file` for read-only investigation.
- `coder` may use `read_file`, `write_file`, and `bash` for implementation work.

Consequently, file changes and shell commands must be performed by the `coder`
subagent, even when the task is initiated interactively by the root agent.
