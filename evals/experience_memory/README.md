# Bounded experience-memory evaluation

This opt-in suite compares memory-disabled and memory-enabled orchestration
using the production `Agent`, `SubagentManager`, structured delegation outcomes,
and memory tools. Everything added lives under `evals/`; runtime source and
production memory configuration are unchanged.

## Commands

Run from the repository root using its existing development environment:

```sh
.venv/bin/python -m evals.experience_memory --help
.venv/bin/python -m evals.experience_memory list
.venv/bin/python -m evals.experience_memory smoke
.venv/bin/pytest -q evals/experience_memory/test_evals.py
```

The smoke command is a deterministic script that deliberately saves a lesson,
recreates the session, recalls it, and passes a selected strategy into delegation.
It tests persistence and orchestration plumbing. Its better first action is
scripted behavior, **not evidence of model learning or memory effectiveness**.

A real-model run first prints a bounded plan without making calls:

```sh
.venv/bin/python -m evals.experience_memory run \
  --provider openai --model YOUR_MODEL --scenario missing_source \
  --repeats 1
```

Add `--execute` to actually run that plan. This can incur provider costs. No
real-model evaluation is launched by help, list, smoke, or a plan-only command.
Reports default to `~/.<harness-name>/evals/experience-memory/<UTC timestamp>-<UUID>/`,
using the runtime's harness name (normally `python-harness`). `HARNESS_NAME` or
`--harness-name NAME` selects the same user-data namespace as the harness.
Every execution chooses a new run directory; prior reports are never overwritten.
Help, list, and plan-only commands create no directories or reports. A dry plan
shows a proposed path; a later execution chooses its own unique path.
Optional custom location: use `--output` only to override the default, supplying
a directory that does not already exist:

```sh
.venv/bin/python -m evals.experience_memory smoke --output ~/Documents/harness-eval-custom-run
```

Providers are `openai`, `anthropic`, and `local`, using the existing adapters.
Cloud adapters use their standard API endpoints and `OPENAI_API_KEY` or
`ANTHROPIC_API_KEY`. `--config-dir DIR` reads configuration from `DIR/.env`
(including the harness name) for real-run plans and execution; smoke reads the
current directory's `.env`. Existing environment variables take precedence;
`--harness-name` overrides the configured name. Local models also use
`DIR/local_models.json` and the
existing `provider/model` identifier. Credentials are not written to reports.
Natural real-model tasks use the runtime's selective memory guidance; they do
not force saving, recall, or a particular lesson. A model doing no saving is an
observable result, not an error hidden by seeding memory.

## Scenarios and isolation

- `exact_handoff`: preserve the exact task ID and input values while following
  a workspace-specific combination convention. Incomplete handoffs can fail
  because the child cannot recover omitted inputs from `inspect_rules`.
- `missing_source`: an unavailable primary source has an approved local fallback.
  Avoiding repeated failed primary actions is observable.
- `unverified_completion`: a provisional acknowledgement does not produce a
  persisted result. Correct completion requires the proper strategy and a
  subsequent verification of the correct task.

Each scenario/repeat has separate enabled and disabled arms. Each arm performs
one training task and one transfer task. Transfer has a fresh root, child,
provider, manager, history, and mutable fixture state, with different task ID
and values. Only the enabled arm's persisted memory can cross that boundary.
Arms and repeats never share stores. First-arm order alternates by repeat and
is recorded in the report; provider seeds are uncontrolled because the current
provider interfaces do not expose seed configuration.

The fixture tools only inspect synthetic rules, update in-memory task state,
and verify it. There is no shell, workspace file access, global skill discovery,
MCP, external-resource tool, or production memory access. Temporary workspaces
and explicit temporary user-data directories are deleted after the run.
Bounded saved assessments/lessons and selected recall IDs/reasons remain in the
report for manual review.

## Bounds

Defaults apply to the whole suite unless indicated:

| Limit | Default | Allowed maximum |
|---|---:|---:|
| Provider requests, including children | 96 | 128 |
| Admitted tool calls, including delegation and memory | 128 | 256 |
| Provider requests per training/transfer session | 12 | 20 |
| Session deadline, seconds | 45 | 60 |
| Suite deadline, seconds | 180 | 300 |
| Requested output tokens per provider response | 512 | 2048 |
| Repeats | 1 | 3 |

Select one scenario or explicit `all`. Every scenario/repeat schedules four
sessions. CLI options expose these bounds; zero, negative, nonfinite, or excessive
values are rejected. Root and child loops also have eight-turn limits. Each
request is capped at 128000 serialized context bytes; each returned response
at 8192 serialized bytes and eight tool calls. Response limits are checked after
receipt; the provider's output-token limit is the upstream generation bound.

Global cap exhaustion is sticky, including when a child exception becomes a
tool error. Remaining scheduled sessions are emitted as `not_run_budget`, never
silently omitted. Large scenario/repeat selections may exhaust a global cap;
increase it only within the hard maxima or run smaller batches. A deadline
cancels in-flight model work, but an already-started production memory write
settles before cancellation; recorded elapsed time includes any cleanup overrun.
It is not an absolute wall-clock kill guarantee.

## Reports and interpretation

Each run writes `report.json`, `runs.jsonl`, and `report.md`. Plan metadata includes
mode, provider/model, scenarios, repeats, schedule/order, and limits. Per-session
results include status and errors, observed task state, bounded final answer,
delegation text, fixture actions, saved-memory samples, and memory-call metrics.

Task success requires a correct persisted value, verification after the action,
and a matching structured final claim. A plausible answer alone cannot pass.
Correct state without a parseable requested final JSON answer is reported
separately. Failed actions, repeated identical failed actions, extra delegations,
and rules reads are observable counts; extra delegations are not automatically
classified as unnecessary.

The application proxy requires recall of an actual training-record ID from the
same scenario/arm **before** the first action, a correct first action, and
verification. Matching behavior without recall, recall after acting, and unrelated
record hits do not satisfy that proxy. Model-selected tag match is only an
auxiliary metric. Semantic lesson quality and why a model changed its behavior
remain manual-review questions; the proxy does not prove causal learning.

Aggregates retain scheduled and observed denominators and status counts,
including errors, timeouts, cap stops, and no-save outcomes. Paired transfer
deltas are reported only when both arms complete; incomplete pairs have null
deltas. Do not compare surviving successful runs while ignoring stopped arms.

The existing provider responses do not expose exact token usage. Exact input
tokens, output tokens, and cost are therefore `null`. Serialized request context
bytes include system text, messages, and tool definitions on every request;
`estimated_context_tokens` is explicitly bytes divided by four and is not a
billing estimate. No model judge, statistical significance claim, or automatic
production policy change is part of this small suite.
