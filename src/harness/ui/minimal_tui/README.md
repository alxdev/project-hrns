# Minimal TUI

This folder contains the original dependency-light terminal interface. It uses
standard input/output and does not provide persistent history, Rich rendering,
or prompt-toolkit editing.

It implements the same input and output protocols as the richer interface:

```python
from harness.ui.minimal_tui import create_ui

input_adapter, output_renderer = create_ui()
```

The application can therefore use it without changing the agent, commands,
providers, tools, permissions, or compaction code.

