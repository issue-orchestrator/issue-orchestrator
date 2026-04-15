#!/usr/bin/env python3
# Managed by issue-orchestrator harden-repo: block-no-verify helper

"""Shared hook policy for blocking no-verify and restricted commands."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
import sys
from pathlib import Path


@dataclass(frozen=True)
class HookDecision:
    """Decision for a hook evaluation."""

    allowed: bool
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.allowed else 2


def extract_command_from_input(raw: str) -> str:
    """Extract the shell command from hook JSON input."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""

    cmd = ""

    tool_args = data.get("toolArgs")
    if isinstance(tool_args, str):
        try:
            args_data = json.loads(tool_args)
            if isinstance(args_data, dict):
                cmd = args_data.get("command", "")
        except (json.JSONDecodeError, TypeError):
            cmd = ""

    if not cmd:
        tool_input = data.get("tool_input")
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command", "")

    if not cmd:
        cmd = data.get("command", "")

    return cmd if isinstance(cmd, str) else ""


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


def _allow_flag_present(start_dir: Path) -> bool:
    search_dir = start_dir
    while True:
        candidate = search_dir / ".issue-orchestrator" / "allow-no-verify-dry-run"
        if candidate.exists():
            return True
        if search_dir.parent == search_dir:
            return False
        search_dir = search_dir.parent


def evaluate_command(command: str, cwd: Path | None = None) -> HookDecision:
    """Evaluate a command string and return allow/deny decision."""
    if not command:
        return HookDecision(True, "")

    cwd = cwd or Path.cwd()

    if is_dry_run_no_verify_push(command) and _allow_flag_present(cwd):
        return HookDecision(True, "")

    patterns = [
        (
            re.compile(r"git\s+(commit|push).*--no-verify"),
            "BLOCKED: --no-verify is forbidden. Pre-push hooks must run.",
        ),
        (re.compile(r"git\s+--no-verify"), "BLOCKED: --no-verify is forbidden."),
        (
            re.compile(r"git\s+commit.*\s-n\s"),
            "BLOCKED: -n (--no-verify) is forbidden.",
        ),
        (
            re.compile(r"git\s+-c\s+core\.hooksPath=/dev/null"),
            "BLOCKED: Disabling hooks via core.hooksPath is forbidden.",
        ),
        (
            re.compile(r"git\s+config\b[^\n]*\bcore\.hooksPath\b[^\n]*/dev/null\b"),
            "BLOCKED: Disabling hooks via core.hooksPath=/dev/null is forbidden.",
        ),
        (
            re.compile(r"gh\s+pr\s+merge"),
            "BLOCKED: Agents cannot merge PRs. Only humans can merge.",
        ),
        (
            re.compile(r"gh\s+api\s+.*pulls/[0-9]+/merge"),
            "BLOCKED: Agents cannot merge PRs via API. Only humans can merge.",
        ),
    ]

    for pattern, reason in patterns:
        if pattern.search(command):
            return HookDecision(False, reason)

    return HookDecision(True, "")


def evaluate_raw_input(raw: str, cwd: Path | None = None) -> HookDecision:
    """Evaluate raw hook JSON input and return allow/deny decision."""
    command = extract_command_from_input(raw)
    if raw and not command:
        return HookDecision(
            False,
            "BLOCKED: unable to extract command from hook input. Input may be malformed.",
        )
    return evaluate_command(command, cwd=cwd)


def format_cursor_response(decision: HookDecision) -> str:
    if decision.allowed:
        return json.dumps({"permission": "allow"})
    return json.dumps({"permission": "deny", "userMessage": decision.reason})


def format_copilot_response(decision: HookDecision) -> str:
    if decision.allowed:
        return json.dumps({"permissionDecision": "allow"})
    return json.dumps(
        {"permissionDecision": "deny", "permissionDecisionReason": decision.reason}
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("claude", "cursor", "gemini", "copilot"), required=True
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.read()
    decision = evaluate_raw_input(raw, cwd=Path.cwd())

    if args.mode in ("claude", "gemini"):
        if not decision.allowed:
            print(decision.reason, file=sys.stderr)
        return decision.exit_code

    if args.mode == "cursor":
        print(format_cursor_response(decision))
        return 0

    if args.mode == "copilot":
        print(format_copilot_response(decision))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
