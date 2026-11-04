from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from harness.config import parse_config

from .runner import Limits, ProviderSettings, run_suite, write_reports
from .scenarios import SCENARIOS


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bounded experience-memory evaluation. Real runs require --execute; "
        "smoke is scripted plumbing only."
    )
    commands = result.add_subparsers(dest="command")
    commands.add_parser("list", help="List synthetic scenarios; no model calls.")
    for name in ("smoke", "run"):
        command = commands.add_parser(name)
        command.add_argument(
            "--scenario",
            choices=[*SCENARIOS, "all"],
            default="all" if name == "smoke" else None,
            required=name == "run",
        )
        command.add_argument("--repeats", type=int, default=1)
        command.add_argument(
            "--output",
            type=Path,
            help="New directory override; defaults to ~/.<harness-name>/evals/experience-memory/"
            "<unique-run-id>. Existing output is never overwritten.",
        )
        command.add_argument("--harness-name", help="Override the runtime HARNESS_NAME setting.")
        command.add_argument("--max-provider-calls", type=int, default=96)
        command.add_argument("--max-tool-calls", type=int, default=128)
        command.add_argument("--session-calls", type=int, default=12)
        command.add_argument("--session-seconds", type=float, default=45)
        command.add_argument("--total-seconds", type=float, default=180)
        command.add_argument("--output-tokens", type=int, default=512)
        if name == "run":
            command.add_argument(
                "--provider", choices=["openai", "anthropic", "local"], required=True
            )
            command.add_argument("--model", required=True)
            command.add_argument("--config-dir", type=Path, default=Path.cwd())
            command.add_argument(
                "--execute",
                action="store_true",
                help="Actually make model calls. Omit to print the bounded plan only.",
            )
    return result


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.command is None:
        cli.print_help()
        return 0
    if args.command == "list":
        for scenario in SCENARIOS.values():
            print(f"{scenario.name}: {scenario.description}")
        return 0
    config_dir = Path.cwd() if args.command == "smoke" else args.config_dir.resolve()
    load_dotenv(config_dir / ".env")
    if args.output is None:
        config_args = ["--harness-name", args.harness_name] if args.harness_name else []
        data_directory = parse_config(config_args).data_directory
        run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex}"
        args.output = data_directory / "evals" / "experience-memory" / run_id
    else:
        args.output = args.output.expanduser()
    limits = Limits(
        args.max_provider_calls,
        args.max_tool_calls,
        args.session_calls,
        args.session_seconds,
        args.total_seconds,
        args.output_tokens,
    )
    try:
        limits.validate()
        if not 1 <= args.repeats <= 3:
            raise ValueError("repeats must be from 1 to 3")
        if args.output.exists():
            raise ValueError("output directory already exists; choose a new path")
    except ValueError as exc:
        cli.error(str(exc))
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    settings = (
        ProviderSettings()
        if args.command == "smoke"
        else ProviderSettings(args.provider, args.model, args.config_dir.resolve())
    )
    plan = {
        "mode": args.command,
        "provider": settings.provider,
        "model": settings.model,
        "scenarios": names,
        "repeats": args.repeats,
        "scheduled_sessions": len(names) * args.repeats * 4,
        "global_provider_call_cap": limits.provider_calls,
        "global_tool_call_cap": limits.tool_calls,
        "global_seconds": limits.total_seconds,
        "exact_tokens_and_cost": None,
        "output": str(args.output),
    }
    if args.command == "run" and not args.execute:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return 0
    print(json.dumps(plan), file=sys.stderr)
    report = asyncio.run(run_suite(settings, names, args.repeats, limits))
    write_reports(report, args.output)
    print(
        json.dumps(
            {
                "report": str(args.output / "report.json"),
                "summary": str(args.output / "report.md"),
                "provider_calls": report["total_provider_calls"],
                "terminal_limit": report["terminal_limit"],
            }
        )
    )
    return 0 if all(row["status"] == "completed" for row in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
