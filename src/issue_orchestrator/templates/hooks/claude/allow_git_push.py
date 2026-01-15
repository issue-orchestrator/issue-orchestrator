#!/usr/bin/env python3
"""
Detect allowed git push variants for hook preflight.

Rules:
- Return 0 only when the command is:
    git push --dry-run --no-verify
  (flags may appear in any order)
- Return 1 for all other commands.

This is used by hooks to avoid expensive filesystem checks unless needed.
"""

from __future__ import annotations

import shlex
import sys


def _parse_argv(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _is_git_push(argv: list[str]) -> bool:
    return len(argv) >= 2 and argv[0] == "git" and argv[1] == "push"


def is_dry_run_no_verify_push(command: str) -> bool:
    argv = _parse_argv(command)
    if not argv or not _is_git_push(argv):
        return False
    return "--dry-run" in argv and "--no-verify" in argv


def main() -> int:
    if len(sys.argv) < 2:
        return 1
    return 0 if is_dry_run_no_verify_push(sys.argv[1]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
