# Python Harness project context

## Purpose

This repository contains an educational, provider-neutral coding-agent harness
written in Python 3.11+. Keep the core independent from terminal UI details so
other interfaces can be added without changing the agent loop.

## Repository layout

- `src/harness/agent/` contains the asynchronous agent loop.
- `src/harness/provider/` contains provider adapters and the deterministic mock.
- `src/harness/tools/` contains tool protocols and the registry; built-in
  implementations live in `src/harness/tools/handlers/`.
- `tools.json` defines built-in tool metadata and handler mappings.
- `.agents/skills/` contains portable project skills; `skills.json` registers
  their catalog metadata and locations.
- `subagents.json` defines the profiles available in this working directory.
- `src/harness/subagents/` contains profiles, delegation, and lifecycle tracking.
- `src/harness/commands/` contains interactive slash commands.
- `src/harness/compaction/` contains conversation compaction strategies.
- `src/harness/mcp/` contains MCP configuration, authentication, and tool loading.
- `src/harness/ui/` contains terminal-specific input and rendering code.
- `tests/` contains the pytest suite.

## Development commands

```sh
pip install -e '.[dev]'
pytest
ruff check src tests
ruff format --check src tests
mypy src
```

## Architecture rules

- Keep provider integrations behind the provider interfaces and preserve the
  common `Message`, `ProviderResponse`, and tool types.
- `AGENTS.md` is root-orchestrator context. Delegated subagents receive focused
  task instructions from the orchestrator instead of the full project context.
- You are the root agent planner/router. Workspace inspection, file writes, and
  shell execution belong to delegated subagents.
- The explorer subagent is read-only. The coder subagent performs changes and
  shell operations, subject to the active permission policy.
- Tool failures should be represented as tool results so the model can react
  to them; do not hide failures by converting them into successful results.
- Skill activation content must remain protected when conversation history is
  compacted. Do not replace it with an abbreviated summary.
- Keep user-authored tool artifacts outside the repository under
  `~/.<harness-name>/tool-artifacts/`; never overwrite an existing artifact.
- Use the configured `working_directory` for workspace files and commands.
- Keep provider system prompts stable and preserve them when switching models.

## Safety and maintenance

- Do not commit credentials, tokens, or environment-specific secrets.
- Changes should include focused tests and preserve non-interactive mode.
- Prefer small, provider-neutral modules over putting new behavior directly in
  the terminal UI.
