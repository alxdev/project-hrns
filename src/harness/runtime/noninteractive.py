from __future__ import annotations

import json
from pathlib import Path

from harness.agent.loop import Agent, NullEventSink


async def run_once(
    agent: Agent,
    prompt: str,
    *,
    output: str,
    provider: str,
    model: str,
    working_directory: Path,
    compaction: str,
    tools_allowed: bool,
) -> int:
    agent.events = NullEventSink()
    answer = await agent.send(prompt)
    if output == "json":
        print(
            json.dumps(
                {
                    "answer": answer,
                    "provider": provider,
                    "model": model,
                    "working_directory": str(working_directory),
                    "compaction": compaction,
                    "tools_allowed": tools_allowed,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(answer)
    return 0
