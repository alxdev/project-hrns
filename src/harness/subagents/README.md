# Subagents

The subagent package lets the main agent delegate focused work to isolated
agent instances with curated tool sets. Profiles may opt into reuse when they
need stateful conversations.

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
      "max_turns": 10,
      "reuse": false
    }
  }
}
```

The profile name determines its delegation tool, here `delegate_analyst`.
`description`, `system_prompt`, `permission_mode`, and `tools` are required.
Permission modes are `inherit`, `ask`, `auto_allow`, and `deny`.
`max_turns` defaults to 10 and must be a positive integer; `reuse` defaults to
false, giving each delegation fresh isolated state.

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

A profile's `skills` array selects its catalog. The subagent calls
`activate_skill` to load the full instructions, skill directory, and supporting
file inventory. Successful activation adds a protected instruction message;
repeated activation reuses it, and compaction preserves its complete content.
Skills grant neither tools nor permissions, and an empty `skills` array permits
no activation. The root orchestrator receives project `AGENTS.md` context
rather than the subagent skill catalog.

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
current session; `requires_approval` means unassigned. Execution still follows
the permission policy.

Use assigned tools first, then discover and request installed capabilities as
needed within the profile's role. An approved `request_tool` adds the definition
to the current session so the subagent can use it. A separate approval offers
to save access in `subagents.json`. Declining or failing to save leaves current
session access intact but grants no future profile access.

Approved saves choose `tools` or `tool_artifacts` by tool origin and initialize
a missing `tool_artifacts` array only when saving. Saves re-read the file after
approval and replace it atomically, preserving edits made while the prompt was
open. Saved access applies to subsequent delegations in the same process and
after restart, for both native tools and artifacts.

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
Successful replacement refreshes assigned profiles and existing session access,
including active and cached agents. It does not grant access to other profiles or
bypass execution permissions. Saving a new assignment still requires the usual
save approval.

Capability controls return JSON text with `status`, `message`, and `next_step`.
Respect denials without retrying the same request or bypassing them through
another tool. Diagnose runtime, dependency, and credential failures rather than
assuming a missing capability; required skill steps must not be silently skipped.

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

The `gmail` profile inherits the same tool approval policy. Interactive Gmail
actions require approval, and Gmail MCP tools are scoped only to this profile.

## Artifact authoring

Every creation starts with `{"name": "tool_name"}` before generating a definition or
files. Missing tools require explicit preparation consent; `preparation_authorized`
allows a full proposal in that session, working directory, and artifact root only.
Preparation approval names the artifact root and working directory; the harness
rechecks the target and root after consent before recording intent.
Installation always requires its own approval callback, including explicit grants
from configured auto-allow policies; an absent callback denies installation.
Preparation has no write or
execution authority. Missing preparation callbacks deny access, including noninteractive
sessions without an explicitly configured auto-allow policy. Invalid artifacts use
the dedicated name-only archive/recreation gate described above, even with auto-allow.
Definition validation failures retain pending intent for correction; installation denial,
cancellation, other failures, committed installation, or observing an installed tool
clear it. Each corrected proposal still follows normal installation approval.

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
creating session. Other subagents request it
by name using the access and save approvals described above.

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
root agent's first loop and are also included for subagents. Discovery activity
is emitted through the generic event sink; the UI shows the current server and
logs the resulting tool count or the server's error. MCP tools use the same
registry, permission policy, and cancellation path as built-in tools.
