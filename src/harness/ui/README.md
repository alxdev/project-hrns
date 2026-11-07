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

Shell and artifact stderr is represented as `tool_log` transcript entries
under the actual tool name and rendered as literal text with UI styling.
It is not sent to the provider as a separate model request; the command's
structured stdout and final failure result remain the model-facing tool output.

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

Paste normally (Cmd+V on Mac), review the text, and press **Enter** to send.
The Rich prompt joins pasted line breaks with spaces, keeping the whole paste
as one input. Multiline pasted text is not interpreted as a slash command;
single-line commands such as `/memory list` work whether typed or pasted.

Bracketed paste is handled directly. For terminals that send raw pasted text,
Enter has an 80ms grace period to collect the rest of an input burst. Trailing
newlines in that burst do not submit it. This is a bounded input heuristic:
unmarked paste chunks delayed more than 80ms can resemble separate manual inputs.
Approval and credential prompts keep their normal input behavior.

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
