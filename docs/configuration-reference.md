# Configuration reference

This guide contains the detailed model catalogs, workspace context, subagent and orchestrator configuration, skills, tool capabilities, and architecture notes moved from the README.

## Model configuration

Interactive startup and `/models` read model definitions from the configured
workspace when the catalog is present there, otherwise from
`~/.project-hrns/`:

- `local_models.json` contains local model servers.
- `cloud_models.json` contains cloud providers and their models.

The root `local_models.json` and `cloud_models.json` files in the harness
source repository are the templates copied by `project-hrns init`. Workspace
catalogs override the matching user catalog. The files are catalogs; they do
not download models or start servers. A local server must already be running at
the configured URL.

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
/models openai/gpt-4.1-mini     Select GPT-4.1 Mini
/models openai/gpt-5-mini       Select GPT-5 Mini
/models openai/gpt-5.6-luna     Select GPT-5.6 Luna
```

The regular OpenAI provider and OpenAI-compatible local providers are separate
features. Local OpenAI-compatible models belong in `local_models.json`; do not
move them into the regular OpenAI configuration.

For development inside this repository, a local environment can be created with:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m harness
```

The default model is configurable with `HARNESS_MODEL`. The working directory
for tools is configurable with `HARNESS_WORKING_DIR`.

## Project context

At startup, the harness uses `AGENTS.md` from the configured workspace when it
exists; otherwise it uses `~/.project-hrns/AGENTS.md`. The workspace file
replaces the user file rather than combining with it. Its contents are added
only to the root orchestrator's provider system prompt. The file is read once
at startup; restart the harness after editing it. Keep it focused on repository
guidance, and never put API keys or other secrets in it.

The root interprets the user request and delegates a focused task to a fresh
child. The user's conversation history stays with the root. See
[delegation](../src/harness/subagents/README.md#delegation) for scope, context lifetime,
and permissions. The interactive `/context` command reports the root context file.

## Subagent configuration

`subagents.json` in the configured workspace overrides the user-level
`~/.project-hrns/subagents.json`; it defines the available profiles, their
tools, and their skills. See the
[subagent guide](../src/harness/subagents/README.md) for profile configuration,
delegation, and permissions.

## Orchestrator configuration

`orchestrator.json` in the configured workspace overrides the user-level
`~/.project-hrns/orchestrator.json` and assigns the root's capabilities. The
source file lists `delegate_subagent`, `recall_experience`,
`record_experience`, and `activate_skill`; its root
`skills` assignment starts empty. Edit it and restart to run different experiments.
For example, this root can delegate to any configured profile and recall memory, but cannot
record new experiences or use MCP:

```json
{
  "tools": ["delegate_subagent", "recall_experience"],
  "skills": [],
  "mcp_servers": [],
  "max_turns": 20
}
```

When the file exists, `tools`, `skills`, and `mcp_servers` are required arrays of
unique names. Nothing omitted is automatically added. Unknown fields, unknown
tools/skills, duplicate names, or invalid dependencies produce a configuration
error. `delegate_subagent` is required; memory controls are optional.
Adding a profile to `subagents.json` requires no change to this tool list. After
restart, its name and capability summary appear in the delegation tool's catalog.
Calls use `{"profile": "explorer", "task": "Find the configuration files."}`.
Old `delegate_<profile>` entries must be replaced with `delegate_subagent`; startup
reports a migration error rather than retaining hidden aliases.
`max_turns` is an optional positive
integer, defaulting to 20, and limits only the root loop.

Root tools are limited to `delegate_subagent`, the two memory controls, and
`activate_skill`. Native workspace tools, artifact tools, and capability-acquisition
controls such as `request_tool` remain child-only. To assign an orchestration skill,
add its catalog name to `skills` and explicitly include `activate_skill` in `tools`.
The actual `activate_skill` handler must also exist in `tools.json`; deletion or
replacement with another handler is an error. A root skill can guide routing and
assessment; a child skill can guide workspace execution. Assignments are independent,
and neither grants additional tools or permissions. Root activation uses the normal
execution policy, including `--allow-tools` in non-interactive mode. Its full activated
instructions remain protected during compaction; model switches preserve the root's
system prompt and catalog.

`memory.json` remains the feature/limits/write-policy switch and enables memory by
default, including when the file is absent. Set `"enabled": false` to disable it.
Assigned memory tools are inactive when memory is explicitly disabled, rather than
breaking the root configuration.
`/tools` and `/agents` show those inactive assignments and the root configuration mode.
Removing `record_experience` permits recall-only experiments; `/memory` remains a
user command for inspecting and forgetting stored records.

`mcp_servers: ["*"]` explicitly allows every server whose `mcp.json` scope applies
to root; `[]` disables root MCP access. A list such as `["docs"]` allows only those
exact normalized server names. It never expands scope: child-only or unregistered
servers yield no root tools. Discovery remains lazy and excluded servers are not
contacted for the root. Cached child discovery does not bypass this filter. MCP
execution retains its configured approval policy.

For existing workspaces without `orchestrator.json`, compatibility defaults retain
`delegate_subagent`, enabled memory controls, scoped MCP, no root skills, and 20 turns.
`/agents` labels this as legacy defaults. A malformed or incomplete existing file
never falls back to those defaults.

## Skills

Skills provide reusable workflow instructions through `SKILL.md` directories
in the workspace's `.agents/skills/` or the installed
`~/.project-hrns/skills/` directory, with paths cataloged in `skills.json`.
`subagents.json` assigns child skills; `orchestrator.json` separately
assigns root skills. Models activate relevant assigned skills. Skill
assignments require `activate_skill` explicitly in that agent's `tools` array;
missing activation is a configuration error, never an automatic tool addition. Skill
instructions survive compaction and grant no tools or permissions. See
[skills and file access](../src/harness/subagents/README.md#skills-and-file-access)
for setup and the [skill capability walkthrough](skill-capability-walkthrough.md)
for manual scenarios.

## Dynamic tool capabilities

Subagents can discover installed tools, request access for their invocation, and
separately approve saving access to their profile. Authorized tool authoring can create
reusable command-backed artifacts outside the project. The
[subagent guide](../src/harness/subagents/README.md#tool-access-and-persistence)
explains access, persistence, and artifact creation.

## Built-in tool configuration

Built-in tools are described in `tools.json` in the configured workspace, with
`~/.project-hrns/tools.json` as the user-level fallback. The project catalog
replaces the user catalog; if the selected file is missing or contains an
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
executable behavior. For user-authored command-backed tools, see
[artifact authoring](../src/harness/subagents/README.md#artifact-authoring).

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
