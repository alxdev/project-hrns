# Experience memory

Memory is optional, provider-neutral, and scoped to the configured working
directory. It stores readable JSON under the harness's existing user-data
directory. It uses no external database, embeddings, background learning, or
automatic skill generation.

## Enable and control it

The repository's `memory.json` is disabled by default. A missing file also
disables memory without creating storage or adding tools. To enable it, set:

```json
{
  "enabled": true,
  "write_policy": "inherit",
  "max_records": 200,
  "max_recall_records": 5,
  "recall_token_budget": 2000
}
```

Configuration is loaded at startup. `write_policy` accepts `inherit` or `deny`.
`inherit` applies the normal execution policy to root memory writes: interactive
approval when configured, and `--allow-tools` for non-interactive writes.
`deny` forbids writes even when execution would otherwise be allowed. Neither
setting grants workspace execution permission. Recall is read-only.

`max_records` (1–10000) retains the newest experiences. `max_recall_records`
(1–50) and `recall_token_budget` (256–16000) bound each recall. Unknown controls
and malformed settings are errors. The token budget is an approximation using
UTF-8 bytes divided by four, not a model-specific tokenizer. It includes the
serialized result, advisory text, and context snapshot wrapper. A record that
cannot fit is omitted whole; results report omitted-match counts.

User controls share the same memory manager and storage:

```text
/memory status
/memory list
/memory show <record-id>
/memory forget <record-id>
/memory help
```

`forget` is an explicit user action, still subject to `write_policy=deny`. It
removes the stored experience and any optional lesson attached to it, and
clears the current protected memory snapshot. It does not rewrite earlier
conversation tool results. `/clear` clears the conversation and its snapshot,
but does not erase persisted experiences. Disable memory or use `forget` to
control future recall.

## Root tools and evidence

Only the root receives `recall_experience` and `record_experience`. These tools
are not installed in the child tool catalog, so children cannot acquire them
with `request_tool`. The root may pass selected findings in a focused child
task. Root and child system prompts remain stable when switching models.

Recording accepts finished delegation IDs from the current session, plus an
assessment, applicability, tags, and optional lesson:

```json
{
  "run_ids": ["a1b2c3d4"],
  "assessment": {
    "summary": "Unsupported formats need explicit validation.",
    "verification_status": "not_assessed"
  },
  "tags": ["parser", "validation"],
  "applicability": "When processing files in unfamiliar formats.",
  "lesson": "Validate accepted formats before processing input."
}
```

Run IDs must be unambiguous, finalized after lifecycle cleanup, and attributed
to the same workspace. Failed and cancelled runs may supply useful evidence;
their historical lifecycle status stays explicit. Invented, prior-session,
ambiguous, and unfinished IDs are rejected. Callers cannot submit or overwrite
runtime evidence through tool arguments.

Each stored experience has a durable UUID, UTC timestamp, workspace key, and
separate fields for:

- `runtime_evidence`: harness-derived run/profile identity, lifecycle status,
  observation status, tool request/dispatch/result/denial counts, inserted skill
  names, observed tool additions, error types, and capability fingerprint.
- `assessment`: a root-authored summary and interpretation. Verification
  defaults to `not_assessed`; `verified`, `failed`, or `partial` require a
  supporting `rationale`. This remains a root assessment, not independent
  verification by the memory system.
- `lesson`: an optional, separately labeled root-authored generalization.
  Its supporting runtime evidence stays in the same record, so retention
  cannot leave an orphaned lesson without evidence.

The harness does not automatically persist the task prompt, child response,
transcripts, raw error messages, tool arguments, tool outputs, or skill contents.
It copies at most four runs and sixteen entries per evidence list, with explicit
omitted counts. Task completion and dispatch counts never prove verified success.
Keep credentials and other sensitive content out of root-authored assessment
fields too; they are persisted as supplied.

## Recall and context

Recall accepts `query`, `tags`, and `kind` (`all`, `experience`, or `lesson`).
Keyword and tag matches are ranked deterministically; tags restrict matching,
and an empty query/tags selects recent records. Each result explains its matched
keywords/tags or recency. `lesson` selects experiences containing a lesson.

Historical capability fingerprints are compared with current metadata without
triggering tool/MCP discovery. Results say `matches_current_metadata`,
`stale_or_changed`, or `unknown` when no comparison is available. A match does
not guarantee execution permission or current external availability. Current
configuration always wins.

After a complete successful recall tool batch, the root replaces one protected
`memory_snapshot` message. If a batch contains several successful recalls, the
last one wins; an empty successful recall replaces stale matches too. Failed
or denied recall does not replace the snapshot. Ordinary tool exchanges remain
intact and unprotected; full protected skill instructions remain untouched.
Compaction preserves the current snapshot rather than accumulating protected
copies. The system prompt is not rewritten.

Snapshot replacement is stateless and creates a new message list. Cancelling
`Agent.send` restores the pre-send messages, including the prior snapshot,
without a pending recall being injected into a later request.

## Persistence and cancellation

Storage lives at:

```text
~/.<harness-name>/memory/<sha256-of-canonical-workspace>/records.json
```

The file contains a versioned workspace envelope and complete experience
records. A canonical workspace path, including symlink resolution, identifies
the namespace. Other workspaces cannot recall those records. Reads validate
the envelope and complete experience schema; corruption is reported without
silently resetting memory.

Writes use a stable lock file with an exclusive POSIX `flock`, bounded lock
wait, fresh read under lock, a private temporary file, fsync, and atomic
replacement. Readers see a complete old or new file. Independent writers merge
against the latest file instead of overwriting each other's changes. Unsupported
locking platforms fail closed on writes. Storage enforces file/record size and
JSON-shape limits; files use mode 0600.

Once a write starts, cancellation waits for the bounded transaction to settle
before propagating. An already committed experience or deletion persists;
conversation rollback does not undo storage. A committed `forget` clears the
snapshot even if its caller was cancelled. Inspect `/memory list` after an
interrupted write. Process shutdown and crashes may likewise leave either the
old or new complete file, never a partially written replacement.
