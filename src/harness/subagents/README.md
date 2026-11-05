# Subagents

The subagent package lets the main agent delegate focused work to fresh
agent instances with curated tool sets. Each child owns its agent loop for the
current task.

## Profiles

Profiles are loaded from `subagents.json` in the configured working directory.
The file is authoritative: if it is missing or its `profiles` object is empty,
the harness has no subagents. Copy or create it to configure another project.

For example, after installing an artifact named `csv_summary`:

```json
{
  "profiles": {
    "analyst": {
      "description": "Inspect files and summarize CSV data.",
      "system_prompt": "Analyze the assigned files and report findings.",
      "permission_mode": "inherit",
      "tools": ["read_file"],
      "tool_artifacts": ["csv_summary"],
      "skills": ["project-summary"],
      "max_turns": 10
    }
  }
}
```

The profile name determines its delegation tool, here `delegate_analyst`.
`description`, `system_prompt`, `permission_mode`, and `tools` are required.
Permission modes are `inherit`, `ask`, `auto_allow`, and `deny`.
`max_turns` defaults to 10 and must be a positive integer.

The required `tools` array names native tools, including `tool_authoring` when
needed. Optional `tool_artifacts` names user-generated tools and defaults to
`[]`. Both contain strings resolved against installed tools by actual origin.
Unknown names, wrong-field assignments, and duplicates within or across the
arrays are rejected. Both arrays feed the same runtime tool set and follow
the same discovery, approval, and execution policy.

`skills` defaults to `[]`. MCP-only profiles can use `"tools": []` and
`"requires_tools": true` to require tools before running; `requires_tools`
defaults to false. Tool implementations and permission enforcement remain in
Python.

## Skills and file access

The harness discovers portable skills from the project's `.agents/skills/`
and user-wide `~/.agents/skills/`. Each skill directory contains `SKILL.md`
with `name` and `description` frontmatter and may contain supporting files in
any layout. Optional project `skills.json` entries override catalog descriptions
or locations:

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

A profile's `skills` array selects the child's catalog. Each root delegation tool
includes the assigned skill names and concise catalog descriptions for routing;
unassigned skills are excluded. The root may name a skill explicitly in the
delegation `task`. The child model selects and activates relevant assigned skills.

It calls `activate_skill` to load the full instructions, skill directory, and supporting
file inventory. Successful activation adds a protected instruction message;
repeated activation reuses it, and compaction preserves its complete content.
Skills grant neither tools nor permissions, and an empty `skills` array permits
no activation. The root receives project `AGENTS.md` context and may have its own
independent skill assignment in `orchestrator.json`; root activation requires an
explicit `activate_skill` tool and follows the execution policy. Full child skill
instructions are not automatically shared with root. Capability summaries do not
change either agent's system prompt.

For `read_file`, an existing workspace path takes precedence. A missing relative
path falls back to a unique readable file in assigned skill roots; multiple
matches are ambiguous. To select an assigned skill explicitly, use, for example,
`{"path": "references/guide.md", "skill": "project-summary"}`. The relative path
must remain inside that skill and existing read grants, with no workspace
fallback. Absolute paths follow workspace/allowed-root checks. Permission
errors, directories, and dangling workspace symlinks do not trigger fallback.

See the [skill capability walkthrough](../../../docs/skill-capability-walkthrough.md)
for manual activation, approval, failure, and artifact scenarios.

## Tool access and persistence

When installed, `list_available_tools` and `request_tool` are added to each
profile. Discovery lists installed native and artifact tools without executing
commands, granting access, or saving configuration. Invalid artifacts are
reported individually while valid entries remain listed. A discovery failure
does not establish that tools are missing. `available` means assigned to the
current invocation; `requires_approval` means unassigned. Execution still follows
the permission policy.

Use assigned tools first, then discover and request installed capabilities as
needed within the profile's role. An approved `request_tool` adds the definition
to the current invocation so the subagent can use it. A separate approval offers
to save access in `subagents.json`. Declining or failing to save leaves access
intact until that invocation ends but grants no future profile access.

Approved saves choose `tools` or `tool_artifacts` by tool origin and initialize
a missing `tool_artifacts` array only when saving. Saves re-read the file after
approval and replace it atomically, preserving edits made while the prompt was
open. `subagents.json` remains the configuration store. Saved access applies to
subsequent delegations in the same process and after restart, for both native
tools and artifacts.

Artifact definitions load lazily and use a lightweight in-process cache, refreshed
on lookup when filesystem metadata changes. Startup does not fully validate artifact
schemas. Active definitions refresh at the next delegation or successful replacement
callback; an explicit `request_tool` removes an active artifact that is now invalid
or missing instead of reporting it available.
When an invalid artifact is encountered, it is reported as unavailable; the profile
keeps its assignment, other tools remain usable, and `subagents.json` is unchanged.
Repair the artifact or use `tool_authoring` to recreate it. First send only
`{"name": "tool_name", "replace_invalid": true}`. This is an archive/recreation
request, not a creation attempt. The harness explains the invalid reason and
requires explicit interactive consent before replacement code is prepared;
non-interactive runs cannot approve it. Approval immediately archives the original
in `INVALID_tool_name` (with a unique suffix if needed) and returns without creating.
Only then prepare and submit a complete ordinary creation proposal with
`replace_invalid=false`. Installation follows normal creation approval, and execution
is separately approved. Full replacement definitions in the first call are rejected
before the consent callback. Discovery and access requests never archive tools.
Successful replacement refreshes assigned profiles and access in active invocations.
It does not grant access to other profiles or bypass execution permissions.
Saving a new assignment still requires the usual save approval.

Capability controls return JSON text with `status`, `message`, and `next_step`.
Respect denials without retrying the same request or bypassing them through
another tool. Diagnose runtime, dependency, and credential failures rather than
assuming a missing capability; required skill steps must not be silently skipped.

## Delegation

The main agent receives one tool per profile, such as `delegate_explorer` or
`delegate_coder`. Its description is generated from current profile assignments,
the skill catalog, and existing tool/MCP observations. The same summary appears
in `/agents`, including:

- The profile's role, native tools with concise descriptions, and controls such
  as `list_available_tools`, `request_tool`, and `activate_skill` when installed
  and assigned (including controls automatically added by profile loading).
- Assigned artifacts and their last known state: undiscovered, last validated,
  or unavailable. Unassigned native tools and already discovered artifacts are
  labeled requestable only when `request_tool` is assigned.
- Assigned skill names and descriptions, plus configured global and scoped MCP
  servers, discovery status, and discovered MCP tool descriptions.

Summaries never activate skills, load artifact manifests, or start MCP discovery.
They expose neither child tool schemas nor full skill instructions. Availability
is a last observation, not an execution or permission guarantee. Descriptions are
bounded to 180 characters per resource; at most 12 unassigned resources and 12
discovered MCP tools are shown, with explicit omitted counts for larger inventories.
Assigned native tools, controls, artifacts, skills, and MCP servers remain listed.

Descriptions refresh whenever the provider requests tool definitions, including
after saved grants, artifact observations/replacements, or MCP discovery and
configuration changes. Transient grants in a live child's registry are excluded
from future-delegation summaries. Capability summaries themselves add no persistent state.

Each delegation tool still accepts only:

```json
{
  "task": "Find the configuration files and summarize them."
}
```

Every delegation creates a fresh child, which runs its own tool loop to complete
the focused `task`. The root retains previous results and supplies any earlier
requirements or findings needed by later tasks.

The focused `task` defines the child's scope. The child receives that root-authored
task, its configured role, tools and assigned skill catalog, and host notices.
The harness does not automatically append the raw user request.

Children do not inherit root `AGENTS.md` or the root's full conversation history.
The orchestrator passes relevant repository guidance in the focused task, while
profiles define stable role behavior. Delegation grants no tool access or
permission: preparation consent, invalid-artifact archival, installation approval,
execution approval, and file safety checks still apply as described below and in
[tool access and persistence](#tool-access-and-persistence).

Each delegation receives a tracking ID and a completed or failed status.

### Structured outcomes

Delegation tools return a versioned JSON object in `ToolResult.content`. The
input remains `{"task": "..."}`. A successful return has this shape:

```json
{
  "schema_version": 1,
  "run_id": "a1b2c3d4",
  "profile": "analyst",
  "objective": "Summarize the assigned report.",
  "lifecycle_status": "completed",
  "child_response": "The report describes three findings...",
  "verification_status": "not_assessed",
  "resource_usage": {
    "observation_status": "observed",
    "tools": [],
    "tools_omitted": 0,
    "skills_activated": [],
    "skills_omitted": 0,
    "tools_added": [],
    "tools_added_omitted": 0
  },
  "errors": [],
  "errors_omitted": 0,
  "capabilities": {
    "fingerprint": "sha256:...",
    "stage": "before_child_run",
    "resources": [],
    "resources_omitted": 0
  }
}
```

`child_response` preserves the child's returned text exactly, including an empty
response or a reported blocker. It is a model report, not harness-verified evidence.
`completed` means the child loop returned; it does not establish task success.
`verification_status` is always `not_assessed` in this version. Tool failures may
therefore appear in a completed run's evidence without making the delegation itself
an error. A failed run has `ToolResult.is_error=true`, its run ID, and structured
errors; preflight rejections such as an unknown profile or empty task have
`run_id=null` because no run began.

An observer bound to each child invocation forwards the original events unchanged
while recording runtime evidence independently of profile names and tool-call IDs.
Each tool entry contains `name`, `requested`, `execution_started`, `returned_success`,
`returned_error`, and `permission_denied` counts. Execution-start means dispatch
into the tool registry, including unknown-tool and validation failures; it does not
prove that an external operation occurred. Policy denials are counted from a trusted
permission event, while handler errors are counted from `ToolResult.is_error`.
The harness does not parse model claims or tool output to infer these facts.

`skills_activated` counts only successful insertion of new protected skill context.
Repeated activation reuses existing instructions and adds no activation count;
failed, denied, or buffered-but-never-inserted activations do not count.
`tools_added` records observed additions to the invocation registry. Its
`persistence` is `not_assessed`; an addition alone does not prove access was saved.
Cancellation propagates as `CancelledError`, preserves partial evidence in the run
record, restores the original event sink, and clears live tracking. Historical
activation evidence records an insertion even if cancellation later rolls back
the invocation's conversation messages.

Resource evidence contains names, counts, and statuses, excluding tool arguments,
raw results, and skill instructions. Each resource list and capability snapshot
is limited to 64 entries; errors are limited to 20 entries, prioritizing run-level
failures. Explicit omitted counts distinguish truncation from completeness.
Repeated identical errors aggregate their count. Runtime exception messages are
limited to 500 characters; tool-error entries contain no raw output.
The deterministic capability fingerprint covers the full sorted capability metadata,
even when the visible snapshot is truncated. Its stage identifies configuration
before discovery or the profile state immediately before the child runs.

`SubagentManager.delegate()` continues to raise original runtime exceptions by
default. Delegation tools use `capture_errors=True` to serialize failed run records;
cancellation always propagates. The non-interactive JSON wrapper and provider types
are unchanged. Outcomes remain in process; optional [experience memory](../memory/README.md)
stores a bounded runtime projection with a separately labeled root assessment.
No automatic task verification is introduced.

Subagent lifecycle events are emitted through the generic event sink, allowing
different UIs to show or ignore start, completion, and elapsed-time messages.

`SubagentManager.describe()` reports configured profiles, active work, and the
latest delegation records. The interactive `/agents` command displays them alongside
the root's tools and the same capability summaries used for model routing.

## Tool ownership

The root's `orchestrator.json` selects delegation, memory, and skill-activation
controls plus an MCP server allowlist. It cannot call native `read_file`,
`write_file`, or `bash` directly. This enforces a strict planner /
executor boundary:

- `explorer` may use `read_file` for read-only investigation.
- `coder` may use `read_file`, `write_file`, and `bash` for implementation work.

The `coder` profile inherits the execution permission policy. The root agent's
delegation call is routing and does not require approval; in interactive mode,
the actual file operation or shell command is displayed and must be approved
before it runs. In non-interactive mode, it follows the same `--allow-tools`
setting as the root agent.

The `gmail` profile inherits the same tool approval policy. Interactive Gmail
actions require approval, and Gmail MCP tools are scoped only to this profile.

## Artifact authoring

Every creation starts with `{"name": "tool_name"}` before generating a definition or
files. Missing tools require explicit preparation consent; `preparation_authorized`
allows a full proposal in that active invocation, working directory, and artifact
root only. Preparation consent and unsaved tool grants end with the invocation.
Preparation approval names the artifact root and working directory; the harness
rechecks the target and root after consent before recording intent.
Installation always requires its own approval callback, including explicit grants
from configured auto-allow policies; an absent callback denies installation.
Preparation has no write or
execution authority. Missing preparation callbacks deny access, including noninteractive
sessions without an explicitly configured auto-allow policy. Invalid artifacts use
the dedicated name-only archive/recreation gate described above, even with auto-allow.
Malformed full proposals return validation errors while retaining preparation
consent. The child corrects and resubmits them within the same delegation's tool
loop. The full proposal must pass validation before installation approval is
requested; validation does not execute the command. Installation denial,
cancellation, other failures, committed installation, or observing an installed
tool clear preparation intent. Denials do not authorize another attempt.

An authorized subagent with `tool_authoring` can create a reusable command-backed
tool under `~/.<harness-name>/tool-artifacts/<tool-name>/`, containing
`manifest.json` and supporting files. Existing artifacts are protected except for
explicitly approved recreation of invalid artifacts as described above. If
installation fails after archival, the original remains at the reported archive
path; there is no automatic rollback. Approval archives the original before
replacement preparation and validation, so creation failures also leave it archived.
Calls targeting installed valid/native tools return `already_installed` after read-only
checks, without a creation attempt or authoring prompt. The call may still appear in
the transcript; it does not mean creation was attempted. Permission denials still apply.
Concurrent
operations on the same artifact from independent processes are unsupported;
filesystem preservation assumes a single writer. Creation does not modify the harness
installation or target project. The artifact is validated and registered for the
creating invocation. Other subagents request it by name using the access and save
approvals described above.

Authoring and loading validate JSON Schema structure with an explicit root
`"type": "object"`, including nested `properties` and `required` constraints.
This does not guarantee compatibility with every provider's schema subset.
Invalid manifests are rejected on validation; existing files are never migrated
automatically. Creation validates the definition
without running the command or verifying its behavior. Functional verification
requires a separately approved execution. Run representative
input and compare the output with the requested result before claiming completion.

Authoring requires an explicit, authorized choice for a narrowly scoped missing
capability. It is not an automatic fallback or a replacement for native tools,
and never bypasses a denial. If installed but unassigned, request
`tool_authoring` first; if absent, report the limitation. Creation and execution
follow normal tool approval. Review authored commands and files before reuse.

Artifacts use the same `files`/`command` format for any installed runtime,
including Python, Node, or compiled programs. For example, include `main.js`
and set the manifest command to `["node", "{artifact_dir}/main.js"]`.
Commands are exec argument vectors, never implicitly split or interpreted by a
shell. Keep the executable and arguments separate; an executable path containing
spaces stays in one entry. Loading does not require the runtime to be installed.
Launch failures identify the actual executable and distinguish unavailable working
directories, unavailable executables, permissions, and other launch errors. Once
launched, script failures retain stderr and exit codes; they are not retried or
repaired automatically.
The command runs in the target project, reads one JSON object from stdin, and
writes its result to stdout; stderr supplies diagnostics. Simple relative
arguments naming bundled files, such as `main.js`, resolve to the artifact
directory. Use `{workspace_dir}` for an explicit project path.

The command environment provides:

- `HARNESS_WORKSPACE_DIR`: the target project's absolute path.
- `HARNESS_ARTIFACT_DIR`: the artifact's absolute path.
- `HARNESS_SKILL_DIRS`: a JSON object mapping the current subagent's allowed
  skill names to their absolute directories.

The harness name defaults to the installed package name. Choose a storage
namespace with either:

```sh
HARNESS_NAME=my-agent-harness python -m harness
python -m harness --harness-name my-agent-harness
```

For adding native Python handlers to the harness itself, see
[built-in tool configuration](../../../README.md#built-in-tool-configuration).

## MCP tools

MCP servers can be assigned to one profile or made global with `/mcp add`.
Profile-scoped tools are discovered synchronously before the profile's first
agent loop, then cached for the session. Global tools are discovered before the
root agent's first loop only when admitted by its `orchestrator.json` MCP allowlist,
and are also included for subagents according to `mcp.json`. Discovery activity
is emitted through the generic event sink; the UI shows the current server and
logs the resulting tool count or the server's error. MCP tools use the same
registry, permission policy, and cancellation path as built-in tools.
