# UI Architecture

The `ui` package contains framework-neutral UI boundaries:

- `protocols.py` — `InputAdapter` and `OutputRenderer`
- `interactive.py` — `InteractiveRunner`

The shared `harness.transcript` module reduces agent events into framework-neutral
transcript state.

Framework-specific implementations live in separate folders:

```text
ui/
├── protocols.py
├── interactive.py
├── prompt_toolkit_rich/
│   ├── input.py
│   ├── output.py
│   └── create_ui(...)
└── minimal_tui/
    ├── terminal.py
    └── create_ui(...)
```

## Current UI composition

The default UI is created in `main.py` with one factory import and one factory
call:

```python
from harness.ui.prompt_toolkit_rich import create_ui

input_adapter, output_renderer = create_ui(config.history_file)
```

`create_ui` returns the two objects required by `InteractiveRunner`. The agent,
providers, tools, permissions, commands, and compaction manager do not know
which UI implementation was selected.

Agent events are reduced into `Transcript` state before the selected output
adapter is called. The adapter receives a detached `TranscriptUpdateEvent`
containing the changed entry and current activity label, rather than depending
on provider or agent internals. This keeps the UI replaceable while making the
transcript the rendering model.

Streaming-capable providers are used by default. Their text arrives as
transcript updates and is printed incrementally; completion or interruption
closes the output line. Providers that only implement `send()` produce a
completed assistant transcript entry. Streamed tool calls are not exposed to
the UI until the provider has assembled the complete response, so approval
behavior remains unchanged.

The Rich adapter updates its status line as the transcript moves through
thinking, permission checking, tool execution, delegation, and response
streaming. Tool and subagent output is rendered from their transcript entries;
the minimal adapter uses the same updates for plain terminal output. This is
line-oriented rendering, not a full-screen UI.

Ctrl-C during an agent operation cancels its asyncio task. The agent closes its
provider stream, which exits the provider's HTTP streaming context; this sends
the connection close promptly, although the remote service ultimately controls
whether generation stops immediately. Partial assistant text remains in the
transcript with `interrupted` status. Ctrl-C at the prompt continues to cancel
only the current input.

## Switching to the minimal UI

Change only the factory import in `main.py`:

```python
from harness.ui.minimal_tui import create_ui
```

The rest of the composition remains the same. Both factories return an input
adapter and an output renderer with the same protocol-compatible behavior.

## Adding another UI

Create a new folder, implement the two protocols, and expose the same factory:

```python
def create_ui(history_file=None):
    return MyInputAdapter(...), MyOutputRenderer(...)
```

Then change the single factory import in `main.py`. A full-screen framework
with its own event loop may implement its own runner rather than using
`InteractiveRunner`, but it can still reuse the agent and all non-UI modules.
