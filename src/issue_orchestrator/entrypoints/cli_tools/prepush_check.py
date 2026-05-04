"""Pre-push validation check.

This module provides a CLI command for checking validation cache
and running validation if needed, for use in pre-push hooks.

Usage:
    python -m issue_orchestrator.entrypoints.cli_tools.prepush_check

Exit codes:
    0 = validation passed (or no validation configured)
    1 = validation failed
    2 = validation error
"""

import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from ...control.validation import PublishGate
from ...execution import GitWorkingCopy, LocalCommandRunner
from ...infra.runtime_artifacts import filter_runtime_managed_dirty_paths

logger = logging.getLogger(__name__)


def find_worktree_root() -> Path:
    """Find the worktree root by looking for .git."""
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path
    return cwd


DIRTY_CHECK_MODES = {"tracked", "unstaged", "all", "off"}
DIRTY_FILE_LIST_LIMIT = 20


def load_validation_cmd(worktree: Path) -> tuple[Optional[str], int, str]:
    """Load validation configuration from the worktree's config file.

    Reads from .issue-orchestrator/config/ in the worktree.
    This ensures tests are deterministic - no env var leakage from parent processes.

    Args:
        worktree: Path to the worktree root

    Returns:
        Tuple of (command, timeout_seconds, pre_push_dirty_check)
    """
    from ...infra.config import load_runtime_validation_config

    validation_config = load_runtime_validation_config(worktree)

    cmd = validation_config.get("cmd")
    timeout = validation_config.get("timeout_seconds", 300)
    dirty_check = validation_config.get("pre_push_dirty_check", "tracked")
    if cmd:
        return cmd, timeout, dirty_check

    return None, 0, dirty_check


def _print_dirty_files(files: list[str]) -> None:
    """Print dirty file list, clipped for readability."""
    if not files:
        return
    print(f"Dirty files (showing up to {DIRTY_FILE_LIST_LIMIT}):")
    for path in files[:DIRTY_FILE_LIST_LIMIT]:
        print(f"  - {path}")
    if len(files) > DIRTY_FILE_LIST_LIMIT:
        print(f"  ... and {len(files) - DIRTY_FILE_LIST_LIMIT} more")


def _filter_guard_excluded_files(files: list[str], worktree: Path) -> list[str]:
    """Filter out orchestrator runtime metadata from dirty-tree guard checks."""
    return filter_runtime_managed_dirty_paths(files, worktree)


def _run_dirty_guard(worktree: Path, mode: str, verbose: bool) -> Optional[int]:
    """Return exit code if dirty guard should block, else None."""
    if mode not in DIRTY_CHECK_MODES:
        if verbose:
            print(
                "Invalid validation.pre_push_dirty_check value: "
                f"{mode!r} (expected tracked|unstaged|all|off)"
            )
        return 1
    if mode == "off":
        return None
    working_copy = GitWorkingCopy()
    raw_dirty_files = working_copy.list_dirty_files(worktree, mode)
    if raw_dirty_files is None:
        # Enumeration failed — fail closed instead of silently passing
        # the gate (which would happen if we collapsed None to []).
        if verbose:
            print(
                "Could not enumerate dirty files; failing closed. "
                f"(validation.pre_push_dirty_check={mode!r})"
            )
        return 1
    dirty_files = _filter_guard_excluded_files(raw_dirty_files, worktree)
    if dirty_files:
        if verbose:
            if mode == "all":
                print(
                    "Working tree is dirty (tracked or untracked files); "
                    "commit, add, or stash before pushing. "
                    "Override with validation.pre_push_dirty_check."
                )
            else:
                print(
                    "Tracked files are dirty; commit or stash before pushing. "
                    "Ignored files are allowed. "
                    "Override with validation.pre_push_dirty_check."
                )
            _print_dirty_files(dirty_files)
        return 1
    return None


def _run_validation_gate(
    worktree: Path,
    cmd: str,
    timeout: int,
    verbose: bool,
) -> int:
    """Run the publish gate and return exit code."""
    if verbose:
        print(f"Validation configured: {cmd}")

    gate = PublishGate(
        worktree,
        command_runner=LocalCommandRunner(),
        working_copy=GitWorkingCopy(),
        command=cmd,
        timeout_seconds=timeout,
    )
    start = time.monotonic()
    result = gate.check(session_output_dir=_prepush_output_dir(worktree))
    duration = time.monotonic() - start
    logger.info(
        "Pre-push validation completed in %.2fs: allowed=%s cache_hit=%s",
        duration,
        result.allowed,
        result.cache_hit,
    )

    if result.allowed:
        cache_note = " (cached)" if result.cache_hit else ""
        if verbose:
            print(f"Validation passed{cache_note}: {result.reason} (%.2fs)" % duration)
        return 0

    if verbose:
        print(f"Validation failed: {result.reason} (%.2fs)" % duration)
        if result.record and result.record.stderr_path:
            stderr_path = worktree / result.record.stderr_path
            if stderr_path.exists():
                print("\nValidation stderr:")
                print(stderr_path.read_text()[:1000])
    return 1


def _prepush_output_dir(worktree: Path) -> Path:
    """Return a stable worktree-local diagnostics directory for hook validation output."""
    head_sha = GitWorkingCopy().get_head_sha(worktree) or "unknown-sha"
    base_dir = worktree / ".issue-orchestrator" / "diagnostics" / "prepush"
    _prune_prepush_output_dirs(base_dir, keep_names={head_sha}, max_keep=5)
    return base_dir / head_sha


def _prune_prepush_output_dirs(
    base_dir: Path,
    *,
    keep_names: set[str],
    max_keep: int,
) -> None:
    """Bound pre-push diagnostics retention to the current SHA plus recent siblings."""
    if not base_dir.exists():
        return
    children = sorted(
        base_dir.iterdir(),
        key=lambda child: child.stat().st_mtime,
        reverse=True,
    )
    keep_budget = max(max_keep - len(keep_names), 0)
    kept_recent = 0
    for child in children:
        if child.name in keep_names:
            continue
        if kept_recent < keep_budget:
            kept_recent += 1
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def run_prepush_check(verbose: bool = False, dirty_only: bool = False) -> int:
    """Run pre-push validation check.

    This function:
    1. Finds the worktree root
    2. Loads validation config
    3. Checks cache for existing valid result
    4. Runs validation if needed
    5. Returns exit code based on result

    Args:
        verbose: Whether to print status messages
        dirty_only: If True, enforce only dirty-tree policy and skip validation command.

    Returns:
        Exit code (0 = passed, 1 = failed, 2 = error)
    """
    worktree = find_worktree_root()
    cmd, timeout, dirty_check = load_validation_cmd(worktree)

    dirty_result = _run_dirty_guard(worktree, dirty_check, verbose)
    if dirty_result is not None:
        return dirty_result

    if dirty_only:
        if verbose:
            print("Dirty-tree check passed")
        return 0

    if not cmd:
        if verbose:
            print("No validation configured - allowing push")
        return 0

    return _run_validation_gate(worktree, cmd, timeout, verbose)


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run pre-push validation check",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress all output",
    )
    parser.add_argument(
        "--dirty-only",
        action="store_true",
        help="Run only dirty-tree guard (skip validation command)",
    )

    args = parser.parse_args()

    if args.quiet:
        logging.disable(logging.CRITICAL)

    try:
        exit_code = run_prepush_check(verbose=args.verbose, dirty_only=args.dirty_only)
        sys.exit(exit_code)
    except Exception as e:
        if not args.quiet:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
