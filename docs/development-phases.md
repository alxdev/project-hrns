# Development phases and project history

This document preserves the detailed implementation phases and project history formerly included in the README.

## Current status: Phase 8 — Skills and extensible tool capabilities

Phase 1 establishes the smallest useful harness:

- Provider-neutral conversation and tool-call types
- An asynchronous agent loop
- An Anthropic HTTP provider
- An OpenAI-compatible HTTP provider
- A deterministic mock provider for tests
- `read_file`, `write_file`, and `bash` tools
- Approval before every tool call
- A plain terminal runner
- Tests for the agent loop, tool registry, and permission behavior

Phase 2 adds an independent interactive-shell layer:

- Rich Markdown and panel rendering
- Rich status spinner while the provider is working
- A modular slash-command registry
- `/help`, `/model`, `/models`, `/clear`, `/tools`, `/exit`, and `/quit`

Phase 3 adds an independent conversation-compaction package:

- No-op, sliding-window, and model-summarization strategies
- Safe split points that preserve tool-call/result pairs
- A compaction manager and reports
- Optional JSONL compaction logging
- `/compact [none|sliding|summarize]`
- `/verbose [on|off]`
- `/status` to display the current runtime configuration

At startup, the terminal UI displays the active provider, model, working
directory, compaction strategy, registered tools, and approval behavior.
It also shows `Ctrl-C` to cancel input or the current operation and `Ctrl-D` to exit.

Phase 4 adds two execution modes:

- Interactive input through prompt-toolkit with persistent history
- Non-interactive execution with `--prompt` or `--stdin`
- Plain-text or JSON output for automated use
- Explicit command-line and environment configuration
- Safe non-interactive permissions that deny tool calls by default
- Separate input, output, and interactive-runner UI components
- Framework-specific UI code organized under `ui/prompt_toolkit_rich/` and `ui/minimal_tui/`

The agent loop and existing provider/tool implementations remain unchanged.

Phase 4.5 adds a small development-quality toolchain:

- Ruff for linting, import organization, and formatting checks
- mypy for static type checking of the application source
- Configuration for both tools in `pyproject.toml`

Phase 5 adds an independent subagent layer:

- Reusable `explorer` and `coder` agent profiles
- Curated tool sets for each profile
- One `delegate_subagent` tool that selects any configured profile
- Active and completed subagent tracking
- Isolated agent state, system prompts, permission modes, and lifecycle events
- `/agents` for viewing agent descriptions, available tools, and delegation activity

The root agent uses strict delegation. `orchestrator.json` selects its delegation,
memory, and skill-activation controls and allowed MCP servers. It does not receive
the base `read_file`, `write_file`, or `bash` tools. The `explorer` subagent handles
read-only inspection, while the `coder` subagent handles file changes and shell
commands. This keeps planning/routing separate from workspace execution.
Delegation itself is automatically allowed; approval is reserved for the
subagent's actual file and shell operations.

To start the harness from this repository, first configure a provider key:

```sh
cp .env.example .env
# Edit .env and set your provider API key.
project-hrns
```

In the Rich prompt, paste normally (Cmd+V on Mac), then press Enter to send.
Pasted multiline text is joined with spaces into one prompt; no paste mode is needed.

To use Anthropic instead:

```sh
export HARNESS_PROVIDER=anthropic
project-hrns
```

The application loads configuration from `.env` automatically. `.env` is
ignored by git, so keep provider credentials there and never commit them.
Use `OPENAI_API_KEY` for OpenAI or `ANTHROPIC_API_KEY` for Anthropic.

`log_prompt_sent_to_model` defaults to `false`. Set it to `true` to log the actual
prompts sent to models, including system instructions, to stderr. These logs may expose
user data, file contents, and tool results; keep sensitive data out of shared logs.

Available models are cataloged in `local_models.json` and
`cloud_models.json`. Use `/models` to list them and `/models provider/model` to
switch the active provider and model. In the Rich/prompt-toolkit UI, the
harness opens a keyboard picker at startup and `/models` opens it again later;
entries show whether their configuration is ready, including missing cloud API
key environment variables. Cancel exits the application. `/models list` prints
the catalog without opening it.
Cloud catalog entries may specify an `apiKeyEnv` value; the secret itself
remains in the environment rather than in JSON. Start with `--provider local`
to use the local catalog's default model.

## Planned phases

### Phase 1 — Core loop

The current phase. Build and verify the minimal async agent loop, provider
boundary, tools, approval gate, terminal runner, and test seam.

### Phase 2 — Interactive shell

Completed. The Rich frontend and command registry are separate modules. The
basic terminal frontend remains available as a simpler UI implementation.

### Phase 3 — Conversation management

Completed. The implementation lives in `src/harness/compaction/` and is
connected to the agent through one optional `CompactionManager` dependency.

### Phase 4 — Input and package refinement

Completed. The input adapter, output renderer, interactive runner, runtime
configuration, and non-interactive runner are separate modules. The agent loop
does not depend on prompt-toolkit or Rich.

Examples:

```sh
python -m harness --prompt "Summarize this project"
python -m harness --stdin < task.txt
python -m harness --prompt "List the files" --output json
python -m harness --prompt "List the files" --allow-tools
```

Non-interactive runs deny tools by default. Add `--allow-tools` only when the
prompt is trusted and tool execution is intended. JSON output reports the
selected permission mode as `tools_allowed`.

### Phase 5 — Subagents

Completed. The implementation lives in `src/harness/subagents/`. The main
agent routes through `delegate_subagent` while each subagent gets its
own isolated agent instance, system prompt, permission mode, and curated
`ToolRegistry`.

Use `/agents` interactively to inspect each profile's role, native tools, artifacts,
assigned skills, MCP discovery state, and delegation status. The same generated
capability summary is included in the generic delegation tool's profile catalog and
refreshes after saved access grants and runtime discovery. Summaries preserve lazy
loading and distinguish assigned resources, requestable tools, and unknown or
unavailable capabilities; they do not grant permission. Full child tool schemas
and skill instructions stay with the child. The main agent selects a profile with
the tool's `profile` argument; `task` remains the focused assignment.

Delegation results use a versioned JSON envelope with the exact child response,
run identity, lifecycle status, observed resource usage, and structured errors.
The harness records runtime events without interpreting the child's claims;
`verification_status` remains `not_assessed`, even when the child loop completes.
See [structured outcomes](../src/harness/subagents/README.md#structured-outcomes)
for the schema and evidence limits.

Optional experience memory adds root-only recall and recording of bounded
delegation evidence, root assessments, and lessons. It is disabled by default;
set `"enabled": true` in `memory.json` and restart to enable it. Writes inherit
execution permissions, so non-interactive recording requires `--allow-tools`.
Use `/memory status`, `/memory list`, `/memory show <id>`, and `/memory forget <id>`
to inspect or remove records. See [experience memory](../src/harness/memory/README.md)
for storage, evidence boundaries, budgets, and cancellation semantics.

### Phase 6 — Streaming, cancellation, and richer UI foundations

- 6.1 completed: framework-neutral transcript state reduced from agent events
- 6.2 completed: OpenAI and Anthropic stream text by default through an optional
  provider capability; send-only providers continue using the non-streaming path
- Streamed text is delivered as framework-neutral delta/completion events and
  rendered incrementally by both terminal adapters; tool calls are assembled
  before approval or execution
- 6.3 completed: a runtime operation controller handles Ctrl-C cancellation
  outside the UI; provider streams close their HTTP response context, bash tools
  terminate their process group, subagent records become `cancelled`, and partial
  streamed text remains in the transcript with an `interrupted` status
- Delegated calls are currently executed sequentially by the agent. If several
  delegations are run concurrently by an orchestration layer, normal asyncio
  parent-task cancellation propagates to each awaited child task.
- 6.4 completed: terminal adapters render detached transcript updates, including
  streaming/interrupted assistant text, tool lifecycle, and subagent lifecycle;
  the Rich status line tracks the active work without requiring a full-screen UI

Each phase should preserve the provider-neutral core and update the project
documentation with what was added and how to run it.

### Phase 7 — MCP tools for agents

MCP tools are discovered lazily from configured Streamable
HTTP endpoints. The project-local `mcp.json` stores endpoint names, URLs, and
scope, plus non-secret authentication settings; credentials are resolved from
environment variables. Tool definitions are fetched from the MCP endpoint on first use and
cached for the rest of the process. A server can be scoped to one subagent or
registered with `--global` for all agents, subject to the root's `orchestrator.json`
server allowlist. Admitted global tools are discovered only when the root first receives a prompt;
subagent tools are discovered synchronously before that subagent's first agent
loop. Discovery status is shown in the active UI, and success or server-provided
failure details are logged. Connections are opened for discovery and individual
tool calls, then closed in the same async task that opened them; only the tool
definitions are cached for the session.

The MCP client is installed with the application dependencies. For an editable
development install, use:

```sh
pip install -e '.[dev]'
```

Register a server interactively:

```text
/mcp add https://example.com/mcp --agent researcher --name docs
/mcp add https://example.com/mcp --global --name docs
/mcp list
/mcp remove docs
```

`--agent` and `--global` are mutually exclusive. Global means all configured
agents (including the root agent); otherwise tools are only exposed to the
named subagent. MCP tools are namespaced as `mcp_<server>_<tool>` and pass
through the harness's normal permission and cancellation behavior. Streamable
HTTP connections support unauthenticated endpoints, environment-backed bearer
tokens or arbitrary headers, and OAuth authorization-code flows. When a required
credential environment variable is missing, the harness prompts for it without
echoing the value and keeps it in memory only for the current process. It is not
added to the model context, logs, `mcp.json`, or shell configuration. OAuth access
and refresh tokens are also kept in memory for the current process.
OAuth opens a browser and listens on the configured loopback redirect URI. The
redirect URI must be registered with the OAuth provider and its port must be
available. The MCP SDK handles OAuth discovery, PKCE, state validation, token
exchange, and refresh. For compatibility with OAuth servers whose metadata
differs only by an empty root-path slash, the client permits that exact
same-origin variation; scheme, host, port, query, fragment, and non-root path
mismatches remain rejected. stdio server launching is not included.

Examples (set credentials in the environment, or omit these exports and enter
the values when prompted; never put credential values in `mcp.json`):

```sh
export GOOGLE_CLIENT_ID='your-client-id'
export GOOGLE_CLIENT_SECRET='your-client-secret'
```

```text
# A bearer-token server
/mcp add https://example.com/mcp --agent researcher --name docs --auth bearer --token-env EXAMPLE_MCP_TOKEN

# An API key or custom header (the value is read from the named environment variable)
/mcp add https://example.com/mcp --global --name internal --auth headers --header-env X-API-Key=INTERNAL_MCP_KEY

# Generic OAuth authorization-code server
/mcp add https://example.com/mcp --agent gmailAgent --name mail --auth oauth --client-id-env GOOGLE_CLIENT_ID --client-secret-env GOOGLE_CLIENT_SECRET --redirect-uri http://127.0.0.1:8765/callback --scopes 'scope.one scope.two'
```

OAuth supports confidential clients using `client_secret_post` by default; set
`--token-auth-method client_secret_basic` if the provider requires HTTP Basic
client authentication, or use `none` for a public PKCE client without a client
secret.

OAuth configuration is provider-neutral. For any provider, use the redirect URI
shown in its OAuth application settings and configure matching scopes. No
provider-specific account setup is performed by the harness. The current OAuth
token cache lasts for one process run, so a new run may require authorization
again depending on the provider. As with all MCP tools, calls remain subject to
the configured tool-approval policy.

The built-in `gmail` subagent is scoped to the Gmail MCP registration in the
local `mcp.json`; invoke `delegate_subagent` with `profile: "gmail"`. Google's Gmail MCP is a
Developer Preview and provides email search/read, labels, and draft creation.
It does not expose a direct send-email tool, so the subagent can prepare drafts
for you to review and send in Gmail, but must not claim to have sent a message.
See [Google's Gmail MCP setup and tool reference](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server).
