# Python Harness

The project is being built incrementally. The core is intentionally independent
from the terminal UI so that different interfaces can be added later without
changing the agent loop.

## Current status: Phase 3

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
- `/help`, `/model`, `/clear`, `/tools`, `/exit`, and `/quit`

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

The agent loop and existing provider/tool implementations remain unchanged.

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

The OpenAI provider defaults to `gpt-4o-mini`. Set `HARNESS_MODEL` to choose a
different model. Its configurable `base_url` also makes it suitable for
OpenAI-compatible local servers when constructed directly in Python.

From the project directory, a local development setup can be created with:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m harness
```

The default model is configurable with `HARNESS_MODEL`. The working directory
for tools is configurable with `HARNESS_WORKING_DIR`.

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

- Improved line editing and history
- Non-interactive execution mode
- Stronger tool registration and dependency injection
- Broader automated test coverage

### Phase 5 — Subagents

- Reusable agent instances
- Curated tool sets
- Delegation tools
- Active-subagent tracking

### Phase 6 — Full terminal UI

- Structured event-driven transcript rendering
- Scrollback and streaming output
- Approval dialogs
- Cancellation and live status indicators

Each phase should preserve the provider-neutral core and update this README
with what was added and how to run it.

## Development

Run the test suite with:

```sh
pytest
```
