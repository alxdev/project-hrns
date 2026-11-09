from __future__ import annotations

import argparse
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HarnessConfig:
    harness_name: str
    provider_name: str
    model: str | None
    working_directory: Path
    prompt: str | None
    read_stdin: bool
    output: str
    allow_tools: bool
    history_file: Path
    log_prompt_sent_to_model: bool = False

    @property
    def data_directory(self) -> Path:
        return Path.home() / f".{self.harness_name}"

    @property
    def non_interactive(self) -> bool:
        return self.prompt is not None or self.read_stdin


def parse_config(argv: Sequence[str] | None = None) -> HarnessConfig:
    parser = argparse.ArgumentParser(description="Run the configured agent harness.")
    parser.add_argument(
        "--harness-name",
        default=os.environ.get("HARNESS_NAME", _default_harness_name()),
        help="Name used for this harness's user data directory (or set HARNESS_NAME).",
    )
    parser.add_argument(
        "--provider",
        choices=("openai", "anthropic", "local"),
        default=os.environ.get("HARNESS_PROVIDER", "openai"),
    )
    parser.add_argument("--model", default=os.environ.get("HARNESS_MODEL"))
    parser.add_argument(
        "--working-dir", type=Path, default=Path(os.environ.get("HARNESS_WORKING_DIR", Path.cwd()))
    )
    parser.add_argument("--prompt", help="Run one prompt and exit.")
    parser.add_argument("--stdin", action="store_true", help="Read one prompt from stdin and exit.")
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format for non-interactive mode.",
    )
    parser.add_argument(
        "--allow-tools",
        action="store_true",
        help="Allow tools to run without approval in non-interactive mode.",
    )
    parser.add_argument(
        "--history-file",
        type=Path,
        default=Path(
            os.environ.get("HARNESS_HISTORY_FILE", "~/.python_harness_history")
        ).expanduser(),
    )
    args = parser.parse_args(argv)
    debug_value = os.environ.get("log_prompt_sent_to_model", "false").strip().lower()
    if debug_value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        parser.error("log_prompt_sent_to_model must be true/false, 1/0, yes/no, or on/off")
    if args.harness_name in {".", ".."} or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", args.harness_name
    ):
        parser.error(
            "--harness-name must contain only letters, numbers, dots, underscores, or hyphens"
        )
    if args.prompt is not None and args.stdin:
        parser.error("--prompt and --stdin cannot be used together")
    if args.prompt is None and not args.stdin and args.output == "json":
        parser.error("--output json requires --prompt or --stdin")
    return HarnessConfig(
        harness_name=args.harness_name,
        provider_name=args.provider,
        model=args.model,
        working_directory=args.working_dir.resolve(),
        prompt=args.prompt,
        read_stdin=args.stdin,
        output=args.output,
        allow_tools=args.allow_tools,
        history_file=args.history_file,
        log_prompt_sent_to_model=debug_value in {"true", "1", "yes", "on"},
    )


def _default_harness_name() -> str:
    """Return the stable Project HRNS user-directory name."""
    return "project-hrns"
