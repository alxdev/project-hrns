# Python Harness

The project is being built incrementally. The core is intentionally independent
from the terminal UI so that different interfaces can be added later without
changing the agent loop.

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
- Profile-specific `delegate_explorer` and `delegate_coder` tools
- Active and completed subagent tracking
- Isolated agent state, system prompts, permission modes, and lifecycle events
- `/agents` for viewing agent descriptions, available tools, and delegation activity

The root agent uses strict delegation. Its tool registry contains only
`delegate_explorer` and `delegate_coder`; it does not receive the base
`read_file`, `write_file`, or `bash` tools. The `explorer` subagent handles
read-only inspection, while the `coder` subagent handles file changes and shell
commands. This keeps planning/routing separate from workspace execution.
Delegation itself is automatically allowed; approval is reserved for the
subagent's actual file and shell operations.

Run the application with:

```sh
cp .env.example .env
# Edit .env and set your provider API key.
python -m harness
```

To use Anthropic instead:

```sh
export HARNESS_PROVIDER=anthropic
python -m harness
```

The application loads configuration from `.env` automatically. `.env` is
ignored by git, so keep provider credentials there and never commit them.
Use `OPENAI_API_KEY` for OpenAI or `ANTHROPIC_API_KEY` for Anthropic.

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

## Model configuration

Interactive startup and `/models` read model definitions from two files in the
project root:

- `local_models.json` contains local model servers.
- `cloud_models.json` contains cloud providers and their models.

The files are catalogs; they do not download models or start servers. A local
server must already be running at the configured URL.

### Local models

Add each local server under `provider`. The provider's `options.baseURL` must
point to an API compatible with the configured style. OpenAI-compatible local
servers use `style: "openai-compatible"`:

```json
{
  "provider": {
    "litert": {
      "style": "openai-compatible",
      "name": "LiteRT Local",
      "options": {
        "baseURL": "http://0.0.0.0:9379/v1"
      },
      "models": {
        "gemma-4-E4B-it.litertlm,gpu,11000": {
          "name": "Gemma 4 E4B (GPU)"
        }
      }
    }
  },
  "model": "litert/gemma-4-E4B-it.litertlm,gpu,11000"
}
```

The model identifier must match the identifier expected by the local server.
The top-level `model` is the default used by `--provider local`; the startup
picker can select any model declared in the file. The catalog key shown in the
picker is `local/<provider>/<model-id>`.

For a local server that requires an API key, put it in the provider options as
`apiKey` (or `api_key`). Local provider keys are read from `local_models.json`.
For OpenAI-compatible local servers, the harness sends the key as a Bearer
token.

Start the local server separately, then launch the harness. For example, with
the LiteRT configuration above:

```sh
uvx litert-lm serve --host 0.0.0.0 --port 9379
python -m harness --provider local
```

The startup picker validates the local configuration but does not make an LLM
request to check server connectivity. If the server is not running, the model
can still appear as ready and its first request will fail with the server error.

### Cloud models

Add cloud providers and models to `cloud_models.json`. `apiKeyEnv` is the name
of the environment variable containing the secret, not the secret itself:

```json
{
  "provider": {
    "openai": {
      "name": "OpenAI",
      "models": {
        "gpt-4o-mini": {
          "name": "GPT-4o Mini",
          "apiKeyEnv": "OPENAI_API_KEY"
        }
      }
    },
    "anthropic": {
      "name": "Anthropic",
      "models": {
        "claude-sonnet-4-5": {
          "name": "Claude Sonnet",
          "apiKeyEnv": "ANTHROPIC_API_KEY"
        }
      }
    }
  }
}
```

Provider-level `options` are inherited by its models. Model-level options
override them. For supported cloud providers, `baseURL` can be used to select
an alternate API endpoint. Set the required key before starting:

```sh
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
```

Cloud entries whose key is missing are still shown in the startup picker, but
are marked unavailable with the missing environment variable. The currently
supported cloud providers are OpenAI and Anthropic; unsupported provider names
are shown as unavailable until an adapter is added.

### Selecting models

The startup picker selects the first usable model by default. Use the keyboard
to choose another entry, or cancel to exit. After startup:

```text
/models                         Open the interactive picker
/models list                    Print all catalog entries
/models local/litert/<model>    Select a local model
/models openai/gpt-4o-mini      Select a cloud model
```

The regular OpenAI provider and OpenAI-compatible local providers are separate
features. Local OpenAI-compatible models belong in `local_models.json`; do not
move them into the regular OpenAI configuration.

From the project directory, a local development setup can be created with:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m harness
```

The default model is configurable with `HARNESS_MODEL`. The working directory
for tools is configurable with `HARNESS_WORKING_DIR`.

## Project context

At startup, the harness looks for `AGENTS.md` in the configured working
directory. When present, its contents are appended only to the root
orchestrator's provider system prompt. The file is read once at startup;
restart the harness after editing it. Keep it focused on repository guidance,
and never put API keys or other secrets in it.

Subagents do not receive the full project context. The root orchestrator uses
`AGENTS.md` to understand the repository and passes relevant instructions in
each focused delegation request. Stable role behavior remains defined by the
configured profiles in `subagents.json`; tool resolution and permission
enforcement remain in Python. The interactive
`/context` command reports the root context file.

## Subagent configuration

Subagents are configured in `subagents.json` in the configured working
directory. The file is authoritative: if it is missing or contains an empty
`profiles` object, no subagents are configured. There is no Python profile
fallback.

Each profile names existing built-in tools by name. MCP-only profiles can use
an empty `tools` array with `requires_tools` enabled; their tools are loaded
when the profile is delegated to. Tool implementations and permission checks
remain enforced by Python code.

The repository's `subagents.json` provides the current explorer, coder, and
Gmail profiles. Copy or create this file in another working directory to
configure the profiles available there.

## Skills

For copy-paste manual scenarios covering activation, tool requests and denials,
failure diagnosis, and reusable artifacts, see the
[skill capability walkthrough](docs/skill-capability-walkthrough.md).
The lab fixtures live in `.agents/skills/` and are explicitly registered in
`skills.json`.

Skills use the portable Agent Skills directory convention. The harness
discovers skills from `.agents/skills/` in the configured project and from
`~/.agents/skills/` for user-wide skills. A skill directory contains a
`SKILL.md` with standard `name` and `description` frontmatter; supporting files
may live anywhere below that directory.

The optional project `skills.json` file is a small registry containing only
the skill catalog metadata and location. It can override the description or
location discovered from a standard directory:

```json
{
  "skills": [
    {
      "name": "project-summary",
      "description": "Inspect a project and summarize its structure.",
      "location": ".agents/skills/project-summary"
    }
  ]
}
```

Subagent profiles opt into skills with their `skills` array in
`subagents.json`. The harness adds those skills to that subagent's catalog;
the model calls `activate_skill` when it needs the full `SKILL.md`. Activation
also reports the skill directory and all supporting files, so a skill can use
its own arbitrary layout. Skill instructions are workflow guidance and do not
grant tools or permissions. The root orchestrator does not receive the
subagent skill catalog automatically; it only receives the project
`AGENTS.md` context and delegates focused work.

For `read_file`, an existing workspace path takes precedence. A missing relative
path falls back to a unique readable file in assigned skill roots; multiple
matches return an ambiguity error. Use, for example,
`{"path": "data/check.json", "skill": "lab-resource-check"}` to select an assigned
skill explicitly. This requires a relative path contained in that skill and
within existing read grants, with no workspace fallback. Absolute paths retain
the existing workspace/allowed-root checks. Permission errors, directories, and
dangling workspace symlinks do not trigger fallback.

Each successful activation adds one protected instruction message containing
the complete skill content. Repeated activation reuses that message. Failed
activations and unrelated tool output are not protected. Compaction preserves
the instructions intact while safely dropping old tool exchanges; protected
tool exchanges from older histories are retained as complete pairs. An empty
profile `skills` array permits no skill activation, even if the subagent
requests access to `activate_skill`.

## Dynamic tool capabilities

Every configured subagent receives `list_available_tools` and `request_tool`
when those controls are installed. Discovery lists installed native and artifact
tool names and descriptions without executing commands, granting access, or
saving profile changes. Invalid artifacts are reported per entry while valid
tools remain listed. Directory enumeration errors return `discovery_failed`;
they do not establish that tools are missing. `available` means assigned to this
session; `requires_approval` means unassigned, not a guarantee of policy approval.
If a subagent needs a built-in or artifact tool installed but not in its profile, it
can explain why and request it. The harness asks for approval, adds the tool
to the current subagent session, and returns control to the model so it can
retry with the new definition. A second, separate approval asks whether that
access should be saved to the profile's `tools` array in `subagents.json`.
Requests execute through the configured tool handler and registry, so an
unregistered request tool cannot grant access. Profile saves re-read the file
after approval, serialize updates within the process, and replace the file
atomically to preserve edits made while the prompt was open.

Shared subagent instructions preserve each profile's role and task boundaries:
use assigned tools first, discover missing capabilities, then request installed
tools as needed. Required skill steps must not be silently skipped. Runtime,
dependency, and credential failures need diagnosis; they do not establish a
missing harness tool. Respect denials without repeating the same denied request
or bypassing it through another tool.

Capability controls return JSON text in the existing `ToolResult.content` string,
with `status`, a readable `message`, and `next_step`. Request results distinguish
enabled, already available, not installed, configuration, validation, and load
failures. Authoring reports creation or failure; permission denial reports
`access_denied`. Ordinary tool output remains unchanged. No automatic recovery
loops or extra model calls are introduced. Declining to save access leaves an
enabled tool available in the current session.

The `tool_authoring` tool is separate from ordinary tool activation. An
authorized tool-builder can create a command-backed tool artifact under
`~/.<harness-name>/tool-artifacts/<tool-name>/`, containing a `manifest.json`
and its supporting files. Set the harness identity with `--harness-name` or
`HARNESS_NAME`; by default it uses the installed package name. The artifact
command can use any installed runtime, such as Python, Node, or a compiled
program. The harness sends tool arguments as JSON on stdin, returns stdout as
the tool result, and surfaces stderr as diagnostics. The artifact is validated
and registered for the current session without modifying the harness
installation or the target project; existing artifacts are never overwritten.
Other subagents can request the created tool by name through `request_tool`.
Saved access is available to subsequent delegations in the same process and
when profiles load after restarting, for both native tools and artifacts. Saving
access to a profile remains a separate approval; discovery never grants
access or executes artifact code. Creating and using the artifact still go
through normal tool approval. Review authored commands and files before reuse.
Authoring is an explicit, narrowly scoped supplementary option only when the
capability is truly missing, never an automatic fallback or a replacement for
native tools. Guidance suggests it only if installed; an unassigned authoring
tool must first be requested. Creation never bypasses a denial.

For example, a Node artifact can include `main.js` and use a manifest command
such as `["node", "{artifact_dir}/main.js"]`. The command runs with the target
project as its working directory, reads one JSON object from stdin, and writes
its result to stdout. Simple relative arguments naming bundled files (such as
`main.js`) also resolve to the artifact directory. Commands can use
`{workspace_dir}` for an explicit project path. The environment supplies:

- `HARNESS_WORKSPACE_DIR`: the target project's absolute path.
- `HARNESS_ARTIFACT_DIR`: the artifact's absolute path.
- `HARNESS_SKILL_DIRS`: a JSON object mapping the current subagent's allowed
  skill names to their absolute directories.

The data directory name can also be configured without changing the artifact format:

```sh
HARNESS_NAME=my-agent-harness python -m harness
# or
python -m harness --harness-name my-agent-harness
```

Tool handlers use stdout-style tool results for model-visible structured
output. Bash stderr is surfaced as operational progress in the terminal UI
and transcript, without creating an extra provider request for each line; a
failed command also returns a concise stderr diagnostic in its tool result.
Shell and artifact commands share the process runner in `tools/process.py`,
including stderr streaming and POSIX process-group cancellation. Artifact
command preparation lives in `tools/artifacts.py`; `tools/loader.py` loads and
validates definitions. Artifacts use one `files`/`command` format for every
language, including Python.

## Built-in tool configuration

Built-in tools are described in `tools.json` in the configured working
directory. The catalog is authoritative; if it is missing or contains an
empty `tools` object, no built-in tools are loaded. Each entry supplies the
tool name, model-facing metadata, input schema, and the Python handler module:

```json
{
  "tools": {
    "math": {
      "handler": "math",
      "description": "Perform an arithmetic calculation.",
      "input_schema": {
        "type": "object",
        "properties": {
          "expression": {"type": "string"}
        },
        "required": ["expression"],
        "additionalProperties": false
      }
    }
  }
}
```

To add a tool to this harness:

1. Add `src/harness/tools/handlers/math.py`.
2. Export an async `execute(context, arguments)` function that returns a
   `ToolResult`. Keep filesystem, process, and other execution behavior in
   this Python handler.
3. Add the matching `math` entry to `tools.json`. The `handler` value must
   match the handler filename and must be a simple module name.
4. Add `math` to a profile's `tools` array in `subagents.json` if a subagent
   should be allowed to use it.
5. Restart the harness after changing the JSON catalog or Python handler.

Handlers in the project `tools.json` are trusted Python code loaded from the
harness's `harness.tools.handlers` package. JSON controls the tool definition
exposed to the model; it does not bypass permission checks or create
executable behavior. User-authored handlers are the separate artifacts
described above and are loaded only after the authoring tool creates them.

## Architecture

```text
terminal runner ──┐
                  ├── agent loop ─── provider
approval policy ──┤        │
                  │        └── tool registry ─── tools
event sink ───────┘
```

The agent loop communicates through small protocols and data objects. It does
not import the terminal runner or a rendering library.

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
agent receives only profile-specific delegation tools while each subagent gets its
own isolated agent instance, system prompt, permission mode, and curated
`ToolRegistry`.

Use `/agents` interactively to inspect agent descriptions, available tools, and delegation
status. The main agent can delegate through `delegate_explorer` or
`delegate_coder`.

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

Each phase should preserve the provider-neutral core and update this README
with what was added and how to run it.

### Phase 7 — MCP tools for agents

MCP tools are discovered lazily from configured Streamable
HTTP endpoints. The project-local `mcp.json` stores endpoint names, URLs, and
scope, plus non-secret authentication settings; credentials are resolved from
environment variables. Tool definitions are fetched from the MCP endpoint on first use and
cached for the rest of the process. A server can be scoped to one subagent or
registered with `--global` for all agents, including the root agent. Global
tools are still discovered only when the root agent first receives a prompt;
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
local `mcp.json`; invoke it through `delegate_gmail`. Google's Gmail MCP is a
Developer Preview and provides email search/read, labels, and draft creation.
It does not expose a direct send-email tool, so the subagent can prepare drafts
for you to review and send in Gmail, but must not claim to have sent a message.
See [Google's Gmail MCP setup and tool reference](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server).

## Development

Install the development dependencies with:

```sh
pip install -e '.[dev]'
```

Run the test suite, lint checks, formatting check, and type check with:

```sh
pytest
ruff check .
ruff format --check .
mypy src
```

The same checks can be run together through the `Makefile`:

```sh
make check
```

Individual targets are also available: `make test`, `make lint`,
`make format-check`, `make format`, and `make type-check`.

Ruff can apply safe formatting and import fixes with:

```sh
ruff check . --fix
ruff format .
```
