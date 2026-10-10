# UI Architecture

The top-level `ui` package contains only framework-neutral pieces:

- `protocols.py` — `InputAdapter` and `OutputRenderer`
- `interactive.py` — `InteractiveRunner`

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

