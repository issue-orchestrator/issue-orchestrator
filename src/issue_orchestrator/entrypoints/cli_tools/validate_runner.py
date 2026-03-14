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

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ...infra.env import get_env

_CONFIG_RE = re.compile(
    r"\[validate-timing\] CONFIG "
    r"validate_jobs=(?P<validate_jobs>\S+) "
    r"unit_parallel=(?P<unit_parallel>\S+) "
    r"simulated_parallel=(?P<simulated_parallel>\S+) "
    r"integration_parallel=(?P<integration_parallel>\S+)"
)
_START_RE = re.compile(r"\[validate-timing\] START target=(?P<target>\S+) at=(?P<at>\S+)")
_END_RE = re.compile(
    r"\[validate-timing\] END target=(?P<target>\S+) "
    r"status=(?P<status>-?\d+) elapsed=(?P<elapsed>\d+)s at=(?P<at>\S+)"
)


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


def resolve_git_dir(worktree: Path) -> Path | None:
    """Resolve the git dir for the current worktree without shelling out."""
    dot_git = worktree / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    content = dot_git.read_text(encoding="utf-8").strip()
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    git_dir = Path(content[len(prefix):].strip())
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    return git_dir


def resolve_git_common_dir(worktree: Path) -> Path | None:
    """Resolve the repository's shared git dir for cross-worktree artifacts."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    commondir_file = git_dir / "commondir"
    if not commondir_file.exists():
        return git_dir
    common_dir = Path(commondir_file.read_text(encoding="utf-8").strip())
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return common_dir


def read_head_ref_name(git_dir: Path) -> str | None:
    """Read the current branch name from HEAD when it points at a ref."""
    head_file = git_dir / "HEAD"
    if not head_file.exists():
        return None
    head = head_file.read_text(encoding="utf-8").strip()
    prefix = "ref: refs/heads/"
    if not head.startswith(prefix):
        return None
    return head[len(prefix):]


def read_branch_name(worktree: Path) -> str | None:
    """Best-effort branch name for diagnostics records without git subprocesses."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    branch = read_head_ref_name(git_dir)
    if branch:
        return branch
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is not None:
        return read_head_ref_name(common_dir)
    return None


def get_shared_timings_file(worktree: Path) -> Path | None:
    """Return the shared JSONL timing file path for this repository."""
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is None:
        return None
    return common_dir / "issue-orchestrator" / "validate-timings.jsonl"


def current_branch_name(worktree: Path) -> str | None:
    """Best-effort branch name for diagnostics records."""
    return read_branch_name(worktree)


def append_jsonl(path: Path | None, record: dict[str, object]) -> None:
    """Append one JSON object to a JSONL file, creating parents as needed."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


@dataclass
class ValidateTimingRecorder:
    """Collect and persist per-target validate timings."""

    worktree: Path
    command: str
    run_id: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    branch: str | None = field(init=False)
    output_path: Path | None = field(init=False)
    config: dict[str, str] = field(default_factory=dict)
    starts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.branch = current_branch_name(self.worktree)
        self.output_path = get_shared_timings_file(self.worktree)

    def process_line(self, line: str) -> None:
        config_match = _CONFIG_RE.search(line)
        if config_match:
            self.config = dict(config_match.groupdict())
            return

        start_match = _START_RE.search(line)
        if start_match:
            self.starts[start_match.group("target")] = start_match.group("at")
            return

        end_match = _END_RE.search(line)
        if not end_match:
            return

        target = end_match.group("target")
        record: dict[str, object] = {
            "kind": "target_timing",
            "run_id": self.run_id,
            "command": self.command,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "target": target,
            "status": int(end_match.group("status")),
            "elapsed_seconds": int(end_match.group("elapsed")),
            "started_at": self.starts.pop(target, None),
            "ended_at": end_match.group("at"),
        }
        for key, value in self.config.items():
            record[key] = value
        append_jsonl(self.output_path, record)

    def finalize(self, *, exit_code: int, total_elapsed_seconds: float) -> None:
        record: dict[str, object] = {
            "kind": "run_summary",
            "run_id": self.run_id,
            "command": self.command,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "exit_code": exit_code,
            "total_elapsed_seconds": round(total_elapsed_seconds, 3),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        for key, value in self.config.items():
            record[key] = value
        append_jsonl(self.output_path, record)


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
    timing_recorder = ValidateTimingRecorder(worktree=worktree, command=command)

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
            timing_recorder.process_line(line)
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
    timing_recorder.finalize(exit_code=exit_code, total_elapsed_seconds=duration)

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
