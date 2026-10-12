# Subagents

The subagent package lets the main agent delegate focused work to reusable
agent instances with curated tool sets.

## Profiles

The default profiles are:

- `explorer` — read-only inspection through `read_file`
- `coder` — `read_file`, `write_file`, and `bash`

Profiles are defined independently in `profiles.py`. Add a profile by creating
a `SubagentProfile` with the tools it is allowed to use.

## Delegation

The `delegate_to_agent` tool accepts:

```json
{
  "profile": "explorer",
  "prompt": "Find the configuration files and summarize them."
}
```

The manager reuses the same `Agent` instance for subsequent delegations to the
same profile, so that profile keeps its own conversation history. Each
delegation receives a tracking ID and a completed or failed status.

`SubagentManager.describe()` reports configured profiles, active work, and the
latest delegation records. The interactive `/subagents` command displays it.
