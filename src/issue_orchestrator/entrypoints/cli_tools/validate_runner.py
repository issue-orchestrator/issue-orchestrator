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

from ...infra.config import Config, find_config_file
from ...infra.env import get_env
from ...infra.validation_invocation import ValidationInvocationError, ValidationResolver


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


def run_validation(
    command: str | list[str],
    output_dir: Path,
    worktree: Path,
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> int:
    """Run validation command and capture output.

    Args:
        command: Command to run
        output_dir: Directory to write output to
        worktree: Working directory for command
        env: Environment variables for command
        input_text: Optional stdin payload

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
    with open(output_file, "w", buffering=1) as f:
        process = subprocess.Popen(
            command,
            shell=isinstance(command, str),
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
        )

        # Send input if provided
        if input_text is not None and process.stdin is not None:
            process.stdin.write(input_text)
            process.stdin.close()

        # Stream output to both file and terminal
        assert process.stdout is not None  # For type checker
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            f.write(line)
            f.flush()  # Ensure output is written even if process crashes

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
        help="Validation command to run (overrides config)",
    )

    args = parser.parse_args()

    worktree = find_worktree_root()
    output_dir = get_output_dir(worktree)

    # Determine command to run
    if args.command:
        exit_code = run_validation(args.command, output_dir, worktree)
        sys.exit(exit_code)

    config_path = find_config_file(worktree)
    if not config_path:
        print("ERROR: No validation command configured.", file=sys.stderr)
        sys.exit(2)

    config = Config.load(config_path)
    resolver = ValidationResolver(config)
    agent_label = get_env("AGENT_LABEL")
    try:
        invocation = resolver.resolve(
            worktree=worktree,
            run_dir=output_dir,
            agent_label=agent_label,
            mode="manual",
        )
    except ValidationInvocationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    if not invocation:
        print("ERROR: No validation configured.", file=sys.stderr)
        print("Configure validation.script (or legacy validation.cmd) in .issue-orchestrator/config/*.yaml", file=sys.stderr)
        sys.exit(2)

    exit_code = run_validation(
        invocation.command,
        output_dir,
        worktree,
        env=invocation.env,
        input_text=invocation.input_text,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
