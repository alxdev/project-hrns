"""Command-line entry point for the installed harness."""

import argparse
import asyncio
import os
import re
import sys

from harness.config import _default_harness_name
from harness.config_paths import user_config_directory
from harness.initialization import initialize_user_config
from harness.main import main


def run() -> int:
    """Run the harness CLI and return its process exit code."""
    if sys.argv[1:2] == ["init"]:
        return _run_init(sys.argv[2:])
    return asyncio.run(main())


def _run_init(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="project-hrns init",
        description="Reset the user's Project HRNS configuration to repository defaults.",
    )
    parser.add_argument(
        "--harness-name",
        default=os.environ.get("HARNESS_NAME", _default_harness_name()),
        help="Name used for the user configuration directory (or set HARNESS_NAME).",
    )
    args = parser.parse_args(arguments)
    if args.harness_name in {".", ".."} or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", args.harness_name
    ):
        parser.error(
            "--harness-name must contain only letters, numbers, dots, underscores, or hyphens"
        )
    try:
        directory, reset_files = initialize_user_config(user_config_directory(args.harness_name))
    except (OSError, ValueError) as exc:
        print(f"could not initialize Project HRNS configuration: {exc}", file=sys.stderr)
        return 2
    print(f"Project HRNS configuration: {directory}")
    print("Reset from repository defaults: " + ", ".join(reset_files))
    print("Existing tool artifacts were preserved.")
    return 0
