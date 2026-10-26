# Conversation Compaction

This package manages conversation history before it is sent to the provider.
It keeps the agent loop independent from any particular compaction algorithm.

Every strategy implements the same asynchronous interface:

```python
class CompactionStrategy(Protocol):
    async def compact(self, messages: Sequence[Message]) -> list[Message]: ...
```

The input history is never modified in place. Strategies return a new message
list.

## Why compaction exists

The provider receives the conversation history on every request. As the
conversation grows, requests become more expensive and eventually approach the
provider's context limit. Compaction reduces the history while preserving the
most useful context.

## Strategies

### `NoCompaction`

The default strategy. It returns the complete history unchanged.

Use it when you want predictable behavior or when history management is being
handled elsewhere.

```python
from harness.compaction import NoCompaction

strategy = NoCompaction()
messages = await strategy.compact(messages)
```

### `SlidingWindow`

Keeps the most recent messages and drops older ones. It does not call the
provider, so it is fast and has no additional API cost.

```python
from harness.compaction import SlidingWindow

strategy = SlidingWindow(keep_last=10)
messages = await strategy.compact(messages)
```

The strategy uses `safe_split_point` before removing history. If the requested
cut falls inside a tool-call/tool-result exchange, it moves the boundary back
so the kept history does not begin with an orphaned tool result.

Successful `activate_skill` calls produce a separate protected user message
with the full skill instructions, identified by `kind="skill_activation"`
and `skill_name`. Repeated activation reuses this message. Failures and other
results in the same batch are not protected. Summarization excludes protected
instructions rather than replacing them with a shortened summary. If older
histories contain protected tool messages, their complete tool exchanges are
retained so no orphaned tool results or unanswered calls are introduced.

Sliding windows are simple and predictable, but they permanently discard older
decisions and context.

### `Summarize`

Uses the configured provider to summarize older messages, then keeps the
summary plus the most recent messages.

```python
from harness.compaction import Summarize

strategy = Summarize(
    provider=provider,
    threshold=20,
    keep_recent=6,
)
messages = await strategy.compact(messages)
```

The strategy does nothing until the history reaches `threshold`. Once it runs,
it:

1. Finds a safe split point.
2. Renders the older messages as a transcript.
3. Sends a one-shot summary request without tools.
4. Inserts the summary as a synthetic user message.
5. Preserves the recent messages unchanged.

Summarization preserves more context than a sliding window, but it costs an
additional provider request and depends on the quality of the summary.

### `LoggingCompaction`

Wraps another strategy and writes before/after histories to a JSONL file when
the history changes.

```python
from pathlib import Path

from harness.compaction import LoggingCompaction, SlidingWindow

strategy = LoggingCompaction(
    inner=SlidingWindow(keep_last=10),
    path=Path("logs/compactions.jsonl"),
)
messages = await strategy.compact(messages)
```

Each log line contains the message counts and serialized before/after
histories. The parent directory is created automatically.

## Using `CompactionManager`

The manager owns the active strategy and provides convenient strategy
selection for commands and application wiring.

```python
from harness.compaction import CompactionManager

compaction = CompactionManager(
    provider=provider,
    sliding_keep_last=10,
    summarize_threshold=20,
    summarize_keep_recent=6,
)
```

Pass it to the agent:

```python
from harness.agent import Agent

agent = Agent(
    provider=provider,
    tools=tools,
    permissions=permissions,
    context=context,
    compaction=compaction,
)
```

Before each provider request, the agent calls the manager's active strategy.
The default manager strategy is `NoCompaction`, so adding the manager does not
change behavior until another strategy is selected.

## Selecting a strategy at runtime

The manager can select a configured strategy by name:

```python
compaction.set_mode("sliding")
compaction.set_mode("summarize")
compaction.set_mode("none")
```

It can also run a one-time strategy and return a report:

```python
updated_messages, report = await compaction.run(messages, "sliding")
messages = updated_messages

print(report.before_count)
print(report.after_count)
print(report.changed)
```

When the interactive shell is running, the same operations are available as:

```text
/compact
/compact none
/compact sliding
/compact summarize
/verbose on
/verbose off
```

`/compact` uses the manager's active strategy. The named forms run the selected
strategy once without changing the active strategy.

## Adding a new strategy

Create a class in this package with an asynchronous `compact` method:

```python
from collections.abc import Sequence

from harness.api.types import Message


class TokenBudget:
    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens

    async def compact(self, messages: Sequence[Message]) -> list[Message]:
        # Estimate tokens, find a safe split, and return the retained history.
        return list(messages)
```

The strategy can be used directly, wrapped with `LoggingCompaction`, or made
available through `CompactionManager` by adding a named branch to
`_strategy_for`.

## Important invariant

Never compact through the middle of a tool exchange. An assistant tool call
must remain paired with its corresponding tool result. `safe_split_point` is
provided for every strategy that removes history:

```python
from harness.compaction import safe_split_point

split = safe_split_point(messages, desired_index)
old_messages = messages[:split]
recent_messages = messages[split:]
```

When no safe boundary can be found, the helper returns `0`, allowing the
strategy to leave the history unchanged rather than produce an invalid
conversation.

