"""Command-line entry point for the installed harness."""

import asyncio

from harness.main import main


def run() -> int:
    """Run the harness CLI and return its process exit code."""
    return asyncio.run(main())
