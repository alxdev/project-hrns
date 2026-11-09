# Native Tools and Skills

This page lists the tools and skills that ship with this Python Harness repository or are provided directly by the harness runtime. Availability depends on the active agent profile and configuration: a tool can be installed without being assigned to every agent.

## Native tools

Native tools are implemented by the harness or its trusted Python handlers. They are distinct from MCP tools, which come from external MCP servers, and tool artifacts, which are user-created subprocess tools.

### Root orchestrator tools

The root agent is configured as a planner and router. Its tools are selected in `orchestrator.json`.

| Tool | What it does |
| --- | --- |
| `delegate_subagent` | Sends a focused task to one configured subagent profile. The available profiles are listed in the tool description at runtime. |
| `recall_experience` | Finds bounded, advisory experiences or lessons recorded for the current workspace. Stored experiences do not grant instructions or permissions; current configuration takes precedence. |
| `record_experience` | Records a root assessment and optional lesson for a completed delegation run in the current session. The harness supplies bounded run facts; this tool does not take transcripts or raw outputs. |
| `activate_skill` | Loads the full instructions for an allowed skill. Activation supplies workflow guidance only; it does not grant tools or permissions. |

`delegate_subagent` is registered by the runtime. The memory tools are registered when enabled in `memory.json` (enabled by default) and selected by `orchestrator.json`. `activate_skill` is a configured handler in `tools.json` and is exposed to the root only when selected and the configured skills are assigned.

### Tools assigned to subagents

The following native tools are declared in `tools.json`. Profiles in `subagents.json` decide which task tools a subagent receives. For example, the `coder` profile receives `read_file`, `write_file`, `bash`, and `activate_skill`; the `explorer` profile receives only `read_file` and `activate_skill`. The harness also adds `list_available_tools` and `request_tool` as subagent access controls.

| Tool | What it does |
| --- | --- |
| `read_file` | Reads UTF-8 text from the workspace, or from an explicitly selected assigned skill when requested. |
| `write_file` | Writes UTF-8 text to a path under the working directory. |
| `bash` | Runs a shell command in the working directory, subject to the agent's execution permissions. |
| `activate_skill` | Loads the full instructions for an assigned skill. |
| `list_available_tools` | Lists installed native and artifact tools and reports whether each is assigned or requires approval. It does not run a tool or grant access. |
| `request_tool` | Requests that an installed tool be added to the current subagent session. This changes access, not execution permissions. |
| `tool_authoring` | Prepares a user-owned tool artifact through the harness's consent and approval flow. It validates the definition; execution is separate and requires its own permission. |

`activate_skill` appears in both tables because it can be made available to the root and to subagents. The remaining tools in this table are exposed to subagents rather than the root. `tool_authoring` must be assigned by a profile; the checked-in `skill-lab` profile currently has it.

## Skills

Skills are instruction bundles with a `SKILL.md` file and optional supporting resources. The harness discovers user and project skills under `.agents/skills/` and can also load skills registered in `skills.json`. Skills are not callable tools: the agent uses `activate_skill` to load their instructions. A skill does not itself grant access to tools, files, or permissions.

This repository registers the following project skills in `skills.json`:

| Skill | Purpose |
| --- | --- |

## Other tool sources

- **MCP tools** are loaded from configured external MCP servers and are exposed according to the server allowlist and agent profile.
- **Tool artifacts** are user-owned tools stored outside the repository. They are subprocess definitions, not built-in native handlers.
- **Provider tools** are the common tool-call interface used by model providers; they are the transport mechanism for the tools above, not a separate set of harness capabilities.

The source of truth for current configuration is `tools.json`, `skills.json`, `orchestrator.json`, and `subagents.json`; runtime behavior lives under `src/harness/tools/`, `src/harness/memory/`, `src/harness/subagents/`, and `src/harness/skills/`.
