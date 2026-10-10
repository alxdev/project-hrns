from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class HarnessConfig:
    provider_name: str
    model: str | None
    working_directory: Path
    prompt: str | None
    read_stdin: bool
    output: str
    allow_tools: bool
    history_file: Path

    @property
    def non_interactive(self) -> bool:
        return self.prompt is not None or self.read_stdin


def parse_config(argv: Sequence[str] | None = None) -> HarnessConfig:
    parser = argparse.ArgumentParser(description="Run the Python Harness.")
    parser.add_argument("--provider", choices=("openai", "anthropic"), default=os.environ.get("HARNESS_PROVIDER", "openai"))
    parser.add_argument("--model", default=os.environ.get("HARNESS_MODEL"))
    parser.add_argument("--working-dir", type=Path, default=Path(os.environ.get("HARNESS_WORKING_DIR", Path.cwd())))
    parser.add_argument("--prompt", help="Run one prompt and exit.")
    parser.add_argument("--stdin", action="store_true", help="Read one prompt from stdin and exit.")
    parser.add_argument("--output", choices=("text", "json"), default="text", help="Output format for non-interactive mode.")
    parser.add_argument(
        "--allow-tools",
        action="store_true",
        help="Allow tools to run without approval in non-interactive mode.",
    )
    parser.add_argument(
        "--history-file",
        type=Path,
        default=Path(os.environ.get("HARNESS_HISTORY_FILE", "~/.python_harness_history")).expanduser(),
    )
    args = parser.parse_args(argv)
    if args.prompt is not None and args.stdin:
        parser.error("--prompt and --stdin cannot be used together")
    if args.prompt is None and not args.stdin and args.output == "json":
        parser.error("--output json requires --prompt or --stdin")
    return HarnessConfig(
        provider_name=args.provider,
        model=args.model,
        working_directory=args.working_dir.resolve(),
        prompt=args.prompt,
        read_stdin=args.stdin,
        output=args.output,
        allow_tools=args.allow_tools,
        history_file=args.history_file,
    )
