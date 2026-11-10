#!/bin/sh
# Offline simulations only: no dependencies or credentials are inspected.
case "${1-}" in
  success)
    printf '%s\n' 'SKILL_LAB_COMMAND_V1 PASS'
    printf '%s\n' 'SIMULATED success: local validation only' >&2
    exit 0
    ;;
  dependency-failure)
    printf '%s\n' 'SIMULATED dependency failure: lab_demo_dependency unavailable; no lookup performed' >&2
    exit 42
    ;;
  credentials-failure)
    printf '%s\n' 'SIMULATED credentials failure: lab_demo_token unavailable; no secrets inspected' >&2
    exit 43
    ;;
  *)
    printf '%s\n' 'Usage: check.sh success|dependency-failure|credentials-failure' >&2
    exit 64
    ;;
esac
