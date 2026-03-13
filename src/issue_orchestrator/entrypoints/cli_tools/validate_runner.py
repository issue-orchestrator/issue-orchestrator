"""Validation runner with output capture.

This module provides a CLI that runs validation commands and captures
output to a known location, so agents can find failure details without
re-running tests.

Output location is determined by ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR.
If not set, defaults to .issue-orchestrator/diagnostics/ for direct runs.

On failure, prints the path to the output file so agents know where to look.

Usage:
    python -m issue_orchestrator.entrypoints.cli_tools.validate_runner
    python -m issue_orchestrator.entrypoints.cli_tools.validate_runner --command "pytest tests/"

Exit codes:
    Same as the underlying validation command
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from ...infra.env import get_env


def find_worktree_root() -> Path:
    """Find the worktree root by looking for .git."""
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path
    return cwd


def get_output_dir(worktree: Path) -> Path:
    """Get the output directory for validation output.

    Checks ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR env var first,
    falls back to .issue-orchestrator/diagnostics/ for direct runs.

    Args:
        worktree: Path to the worktree root

    Returns:
        Path to the output directory
    """
    env_dir = get_env("VALIDATION_OUTPUT_DIR")
    if env_dir:
        return Path(env_dir)
    # Fallback for direct runs (not orchestrator-managed)
    return worktree / ".issue-orchestrator" / "diagnostics"


def load_validation_cmd(worktree: Path) -> str | None:
    """Load validation command from config.

    Args:
        worktree: Path to the worktree root

    Returns:
        Validation command string, or None if not configured
    """
    from ...infra.config import load_validation_config

    validation_config = load_validation_config(worktree)
    return validation_config.get("cmd")


def run_validation(command: str, output_dir: Path, worktree: Path) -> int:
    """Run validation command and capture output.

    Args:
        command: Command to run
        output_dir: Directory to write output to
        worktree: Working directory for command

    Returns:
        Exit code from the command
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "validation-output.log"
    is_orchestrated_run = get_env("VALIDATION_OUTPUT_DIR") is not None
    line_count = 0
    byte_count = 0

    print(f"Running: {command}")
    print(f"Output will be saved to: {output_file}")
    if is_orchestrated_run:
        print("[orchestrated] full output -> file; terminal shows lifecycle markers only")
    print()

    start = time.monotonic()

    # Run command, capturing output while also displaying it
    # Use line buffering (buffering=1) to ensure output is written immediately
    with open(output_file, "w", buffering=1) as f:
        f.write(f"[validate_runner] start pid={os.getpid()} cwd={worktree} command={command}\n")
        f.flush()
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
        )
        child_pid = process.pid
        start_marker = f"[validate_runner] child_started pid={child_pid}\n"
        sys.stdout.write(start_marker)
        sys.stdout.flush()
        f.write(start_marker)
        f.flush()

        # Stream output to both file and terminal
        assert process.stdout is not None  # For type checker
        for line in process.stdout:
            line_count += 1
            byte_count += len(line.encode("utf-8", errors="replace"))
            if not is_orchestrated_run:
                sys.stdout.write(line)
                sys.stdout.flush()
            f.write(line)
            f.flush()  # Ensure output is written even if process crashes

        eof_marker = (
            f"[validate_runner] stdout_eof pid={child_pid} "
            f"lines={line_count} bytes={byte_count} elapsed={time.monotonic() - start:.1f}s\n"
        )
        sys.stdout.write(eof_marker)
        sys.stdout.flush()
        f.write(eof_marker)
        f.flush()

        while True:
            try:
                process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                wait_marker = (
                    f"[validate_runner] waiting_for_exit pid={child_pid} "
                    f"elapsed={time.monotonic() - start:.1f}s after_stdout_eof\n"
                )
                sys.stdout.write(wait_marker)
                sys.stdout.flush()
                f.write(wait_marker)
                f.flush()

    duration = time.monotonic() - start
    exit_code = process.returncode
    exit_marker = (
        f"[validate_runner] child_exited pid={child_pid} exit_code={exit_code} "
        f"elapsed={duration:.1f}s lines={line_count} bytes={byte_count}\n"
    )
    # The exit marker is computed only after the child has fully exited and the
    # main streaming file handle is closed, so append it in a short final write.
    with open(output_file, "a", encoding="utf-8") as f:
        f.write(exit_marker)
    sys.stdout.write(exit_marker)
    sys.stdout.flush()

    print()
    if exit_code == 0:
        print(f"Validation PASSED (exit code 0) in {duration:.1f}s")
        print(f"Full output saved to: {output_file}")
    else:
        print("=" * 60)
        print(f"Validation FAILED (exit code {exit_code}) in {duration:.1f}s")
        print("=" * 60)
        print()
        print("Full output saved to:")
        print(f"  {output_file}")
        print()
        print(f"To view: cat {output_file}")
        print("=" * 60)

    return exit_code


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run validation with output capture",
    )
    parser.add_argument(
        "--command", "-c",
        help="Validation command to run (default: from config)",
    )

    args = parser.parse_args()

    worktree = find_worktree_root()
    output_dir = get_output_dir(worktree)

    # Determine command to run
    command = args.command
    if not command:
        command = load_validation_cmd(worktree)
    if not command:
        print("ERROR: No validation command configured.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Either:", file=sys.stderr)
        print("  1. Pass --command 'your command here'", file=sys.stderr)
        print("  2. Configure validation.cmd in .issue-orchestrator/config/*.yaml", file=sys.stderr)
        sys.exit(2)

    exit_code = run_validation(command, output_dir, worktree)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
