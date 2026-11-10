---
name: lab-command-check
description: Exercise native bash acquisition and diagnose simulated local command failures.
---

Use the requested mode: `success`, `dependency-failure`, or `credentials-failure`.
Read `scripts/check.sh` relative to the directory returned by activation. Discover
installed tools with list_available_tools; if bash is unassigned, request_tool
for `bash` with the reason that this task explicitly exercises a bundled local
command. After approval, execute `sh '<skill_directory>/scripts/check.sh' <mode>`
through bash within the same delegation. Quote the resolved path safely.

Respect denied access or execution: report the denial and stop; do not switch
tools to bypass it. Report stdout, stderr, and exit status. The helper's failures
are simulations, not evidence that a harness tool is missing. Diagnose the
labelled dependency or credentials failure and stop. Do not author a tool,
install anything, read credentials, modify configuration, or contact a network.
