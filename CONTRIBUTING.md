# Contributing

Thanks for helping improve Project HRNS. This guide explains how to propose changes and prepare them for review.

## Branch workflow

- `dev` is the integration branch. Feature and fix pull requests should target `dev`.
- `master` is the stable branch for releases. Do not open regular feature pull requests against `master`.

To contribute, fork the repository, create a focused branch from `dev`, make your changes, and open a pull request from your fork back to this repository's `dev` branch. If GitHub selects `master` as the pull request base by default, change the base branch to `dev` before submitting.

Open an issue before starting a large change or one that affects architecture, provider interfaces, JSON resource formats, or orchestration and memory behavior. This gives us a chance to agree on the approach before you invest in the implementation.

## Set up and check your changes

Use Python 3.11 or newer. From the repository root, install the project and development dependencies:

```sh
pip install -e '.[dev]'
```

Run the project's checks before opening a pull request:

```sh
pytest
ruff check src tests
ruff format --check src tests
mypy src
```

For behavior changes, include focused tests where practical. If a check cannot be run, mention that in the pull request description.

## Project conventions

- Keep the core provider-neutral. Provider-specific behavior belongs behind the provider interfaces, which preserve the common message, response, and tool types.
- The root JSON resource files are the source templates used by `project-hrns init`. Keep tool, skill, subagent, orchestrator, memory, MCP, and model configuration changes reflected in the appropriate JSON files and document user-visible changes.
- Do not commit API keys, tokens, personal configuration, or other environment-specific secrets.
- Keep documentation in sync with changes to commands, configuration, tools, skills, and orchestration behavior.

## Pull request description

Please include:

- The problem or use case addressed.
- A summary of the change and any effect on configuration or user-visible behavior.
- The checks you ran and their results.
- Any follow-up work or limitations reviewers should know about.

Keep each pull request focused so it can be reviewed and merged independently.

## Releases and hotfixes for maintainers

The maintainer prepares releases from `dev` after the integrated changes are stable and checks pass. Before release, update the version in `pyproject.toml` and prepare release notes when applicable. Merge `dev` into `master`, then tag the release commit with its version (for example, `v0.2.0`). Bring any release fixes back into `dev` so both branches stay aligned.

For an urgent fix to an existing release, branch from `master`, submit and review the fix, then merge it into both `master` and `dev`.
