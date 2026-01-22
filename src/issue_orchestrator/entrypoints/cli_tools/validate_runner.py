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

    print(f"Running: {command}")
    print(f"Output will be saved to: {output_file}")
    print()

    start = time.monotonic()

    # Run command, capturing output while also displaying it
    with open(output_file, "w") as f:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
        )

        # Stream output to both file and terminal
        assert process.stdout is not None  # For type checker
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            f.write(line)

        process.wait()

    duration = time.monotonic() - start
    exit_code = process.returncode

    print()
    if exit_code == 0:
        print(f"Validation PASSED (exit code 0) in {duration:.1f}s")
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
