#!/usr/bin/env python3
"""Tiny spike: run a Claude Code session via subprocess (no tmux).

This is intentionally minimal and only meant to validate that:
- we can launch a Claude CLI command in a normal subprocess
- we can capture logs to a file
- we can observe completion record creation (if the agent writes it)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path

def _resolve_prompt(worktree: Path, prompt_arg: str) -> Path:
    prompt_path = Path(prompt_arg)
    if not prompt_path.is_absolute():
        prompt_path = (worktree / prompt_path).resolve()
    return prompt_path


def _escape_single_quotes(text: str) -> str:
    return text.replace("'", "'\\''")


def _build_command(
    prompt_path: Path,
    initial_prompt: str,
    prompt_mode: str,
    model: str,
    permission_mode: str,
    claude_args: str,
) -> str:
    escaped_prompt = _escape_single_quotes(initial_prompt)
    escaped_prompt_path = _escape_single_quotes(str(prompt_path))
    claude_args = claude_args.strip()
    args = f" {claude_args}" if claude_args else ""

    if prompt_mode == "stdin":
        return (
            f"printf '%s' '{escaped_prompt}' | "
            f"claude{args} --input-format text --permission-mode {permission_mode} --model {model} "
            f"--append-system-prompt 'Read {escaped_prompt_path} for your instructions.'"
        )

    return (
        f"claude{args} --permission-mode {permission_mode} --model {model} "
        f"--append-system-prompt 'Read {escaped_prompt_path} for your instructions.' '{escaped_prompt}'"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Spike: run Claude via subprocess.")
    parser.add_argument(
        "--worktree",
        default=str(Path.cwd()),
        help="Worktree path to run in (default: cwd).",
    )
    parser.add_argument(
        "--prompt",
        default=".issue-orchestrator/prompts/simple-fix.md",
        help="Prompt file path (relative to worktree by default).",
    )
    parser.add_argument(
        "--command",
        default="",
        help="Override command string (if omitted, uses AgentConfig).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout seconds before terminating the process.",
    )
    parser.add_argument(
        "--completion-path",
        default=".issue-orchestrator/completion-subprocess.json",
        help="Relative path for completion record.",
    )
    parser.add_argument(
        "--model",
        default="sonnet",
        help="Claude model to use.",
    )
    parser.add_argument(
        "--permission-mode",
        default="default",
        help="Claude permission mode (default/acceptEdits/bypassPermissions/plan/dontAsk).",
    )
    parser.add_argument(
        "--claude-args",
        default=os.environ.get("ORCHESTRATOR_CLAUDE_ARGS", ""),
        help="Extra claude args (defaults to ORCHESTRATOR_CLAUDE_ARGS).",
    )
    parser.add_argument(
        "--prompt-mode",
        default="stdin",
        choices=["stdin", "arg"],
        help="Claude prompt mode to use for AgentConfig rendering.",
    )
    parser.add_argument(
        "--initial-prompt",
        default=(
            "This is a subprocess spike. Please print a short confirmation, "
            "then run agent-done completed --implementation \"subprocess spike\" --problems \"\" "
            "and exit with /exit."
        ),
        help="Initial prompt passed to the agent.",
    )
    args = parser.parse_args()

    worktree = Path(args.worktree).resolve()
    prompt_path = _resolve_prompt(worktree, args.prompt)

    if args.command:
        command = args.command
    else:
        command = _build_command(
            prompt_path,
            args.initial_prompt,
            args.prompt_mode,
            args.model,
            args.permission_mode,
            args.claude_args,
        )

    env = os.environ.copy()
    env["ISSUE_ORCHESTRATOR_COMPLETION_PATH"] = args.completion_path
    env["ISSUE_ORCHESTRATOR_CLAUDE_PROMPT_MODE"] = args.prompt_mode
    env["PATH"] = f"{worktree / '.venv' / 'bin'}:{worktree / 'scripts'}:{env.get('PATH', '')}"

    full_cmd = f'cd "{worktree}" && {command}'

    log_dir = worktree / ".issue-orchestrator" / "spikes"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "claude-subprocess.log"

    start = time.monotonic()
    timed_out = False

    with open(log_path, "a") as log_f:
        proc = subprocess.Popen(
            full_cmd,
            shell=True,
            executable="/bin/bash",
            cwd=str(worktree),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            returncode = proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                returncode = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait(timeout=10)

    elapsed = time.monotonic() - start
    completion_path = worktree / args.completion_path
    completion_exists = completion_path.exists()

    print("Subprocess spike summary:")
    print(f"- command: {command}")
    print(f"- returncode: {returncode} (timed_out={timed_out})")
    print(f"- elapsed_s: {elapsed:.1f}")
    print(f"- log_path: {log_path}")
    print(f"- completion_path: {completion_path} (exists={completion_exists})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
