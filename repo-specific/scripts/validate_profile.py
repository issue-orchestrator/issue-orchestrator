#!/usr/bin/env python3
"""Profile validate bottlenecks using isolated cold runs.

Each run executes in a detached HEAD worktree rooted at the current committed
revision. Uncommitted local changes are intentionally excluded for reproducible
baselines.

Each isolated worktree is provisioned via `make worktree-setup` before running
the measured target, so runtime behavior matches real worktree usage.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_VALIDATE_TARGETS = [
    "typecheck",
    "lint-arch",
    "lint-complexity",
    "test-unit",
    "test-simulated",
    "test-integration",
    "test-web",
]


@dataclass
class CommandResult:
    name: str
    command: list[str]
    wall_seconds: float
    exit_code: int
    worktree_path: str | None = None


def detect_jobs() -> int:
    cpu_count = os.cpu_count()
    if cpu_count is None or cpu_count <= 0:
        return 5
    return cpu_count


def run_command(
    name: str,
    command: list[str],
    dry_run: bool,
    cwd: Path | None = None,
    worktree_path: str | None = None,
) -> CommandResult:
    cwd_info = f" (cwd={cwd})" if cwd is not None else ""
    print(f"[profile] {name}: {' '.join(command)}{cwd_info}")
    if dry_run:
        return CommandResult(
            name=name,
            command=command,
            wall_seconds=0.0,
            exit_code=0,
            worktree_path=worktree_path,
        )

    start = time.monotonic()
    completed = subprocess.run(command, check=False, cwd=cwd)
    wall = time.monotonic() - start
    return CommandResult(
        name=name,
        command=command,
        wall_seconds=wall,
        exit_code=completed.returncode,
        worktree_path=worktree_path,
    )


def discover_validate_targets(make_bin: str) -> list[str]:
    """Read _validate-impl prerequisites from make's expanded database."""
    proc = subprocess.run(
        [make_bin, "-pn"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return list(DEFAULT_VALIDATE_TARGETS)

    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("_validate-impl:"):
            continue
        _, _, deps_part = line.partition(":")
        deps = [token for token in deps_part.strip().split(" ") if token]
        if deps:
            return deps
        break

    return list(DEFAULT_VALIDATE_TARGETS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--make-bin",
        default=os.environ.get("GMAKE", "make"),
        help="GNU make executable to use (default: GMAKE env or make)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=int(os.environ.get("VALIDATE_JOBS", detect_jobs())),
        help="Parallel job count to use for parallel validate profile run",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write JSON report to this path "
            "(default: <repo-root>/.issue-orchestrator/diagnostics/validate-profile-<timestamp>.json)"
        ),
    )
    parser.add_argument(
        "--no-vscode",
        action="store_true",
        help="Skip test-vscode from profiling runs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them",
    )
    parser.add_argument(
        "--targets",
        help=(
            "Comma-separated target override. "
            "If omitted, targets are discovered from _validate-impl."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root (default: inferred from this script location)",
    )
    return parser.parse_args()


def default_output_path(repo_root: Path) -> Path:
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return repo_root / ".issue-orchestrator/diagnostics" / f"validate-profile-{ts}.json"


def collect_failures(results: list[CommandResult]) -> list[CommandResult]:
    failed = [result for result in results if result.exit_code != 0]
    if failed:
        print("[profile] failed command(s):", file=sys.stderr)
        for result in failed:
            print(
                f"  - {result.name} (exit={result.exit_code})",
                file=sys.stderr,
            )
    return failed


def emit_summary(
    target_results: list[CommandResult],
    validate_raw_result: CommandResult,
    jobs: int,
) -> dict[str, object]:
    serial_total = sum(result.wall_seconds for result in target_results)
    cold_parallel_total = validate_raw_result.wall_seconds
    max_target = max((result.wall_seconds for result in target_results), default=0.0)
    bottleneck_gap = cold_parallel_total - max_target

    sorted_targets = sorted(target_results, key=lambda r: r.wall_seconds, reverse=True)
    top_targets = sorted_targets[:3]

    summary = {
        "timestamp_utc": datetime.now(tz=UTC).isoformat(),
        "jobs": jobs,
        "cold_validate_raw_seconds": cold_parallel_total,
        "cold_target_sum_seconds": serial_total,
        "cold_slowest_target_seconds": max_target,
        "cold_validate_minus_slowest_target_seconds": bottleneck_gap,
        "top_targets": [asdict(result) for result in top_targets],
    }

    print()
    print("Validate Profile Summary")
    print("------------------------")
    print(f"jobs: {jobs}")
    print(f"cold validate-raw: {cold_parallel_total:.2f}s")
    print(f"cold target sum: {serial_total:.2f}s")
    print(f"cold slowest target: {max_target:.2f}s")
    print(f"validate-raw minus slowest target: {bottleneck_gap:.2f}s")
    print("top targets:")
    for result in top_targets:
        print(f"  - {result.name}: {result.wall_seconds:.2f}s")
    return summary


def prepare_worktree(
    make_bin: str,
    name: str,
    worktree: Path,
    dry_run: bool,
) -> CommandResult:
    """Prepare a fresh worktree exactly like real agent/user setup."""
    return run_command(
        name=f"{name}:worktree-setup",
        command=[make_bin, "worktree-setup"],
        dry_run=dry_run,
        cwd=worktree,
        worktree_path=str(worktree),
    )


def run_in_isolated_worktree(
    repo_root: Path,
    make_bin: str,
    name: str,
    make_target: str,
    dry_run: bool,
    jobs: int | None = None,
) -> CommandResult:
    tmp_dir = Path(tempfile.mkdtemp(prefix="io-validate-profile-"))
    worktree = tmp_dir / "wt"
    try:
        add_cmd = ["git", "-C", str(repo_root), "worktree", "add", "--detach", str(worktree), "HEAD"]
        add_result = run_command(
            name=f"{name}:worktree-add",
            command=add_cmd,
            dry_run=dry_run,
        )
        if add_result.exit_code != 0:
            return CommandResult(
                name=name,
                command=add_cmd,
                wall_seconds=add_result.wall_seconds,
                exit_code=add_result.exit_code,
                worktree_path=str(worktree),
            )
        setup_result = prepare_worktree(
            make_bin=make_bin,
            name=name,
            worktree=worktree,
            dry_run=dry_run,
        )
        if setup_result.exit_code != 0:
            return CommandResult(
                name=name,
                command=setup_result.command,
                wall_seconds=setup_result.wall_seconds,
                exit_code=setup_result.exit_code,
                worktree_path=str(worktree),
            )

        make_cmd = [make_bin]
        if jobs is not None:
            make_cmd.append(f"-j{jobs}")
        # output-sync is useful for parallel aggregate runs; unnecessary for single-target runs.
        if jobs is not None:
            make_cmd.append("--output-sync=target")
        make_cmd.append(make_target)
        return run_command(
            name=name,
            command=make_cmd,
            dry_run=dry_run,
            cwd=worktree,
            worktree_path=str(worktree),
        )
    finally:
        _ = run_command(
            name=f"{name}:worktree-remove",
            command=["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree)],
            dry_run=dry_run,
        )
        if not dry_run:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    args = parse_args()
    make_bin = args.make_bin
    repo_root = args.repo_root.resolve()
    include_vscode = not args.no_vscode
    if args.output is not None:
        output_path = args.output if args.output.is_absolute() else repo_root / args.output
    else:
        output_path = default_output_path(repo_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.targets:
        target_list = [target.strip() for target in args.targets.split(",") if target.strip()]
    else:
        target_list = discover_validate_targets(make_bin)
    if include_vscode and "test-vscode" not in target_list:
        target_list.append("test-vscode")

    target_results: list[CommandResult] = []
    for target in target_list:
        target_results.append(
            run_in_isolated_worktree(
                repo_root=repo_root,
                make_bin=make_bin,
                name=f"target:{target}",
                make_target=target,
                dry_run=args.dry_run,
            ),
        )

    validate_raw_result = run_in_isolated_worktree(
        repo_root=repo_root,
        make_bin=make_bin,
        name="parallel:validate-raw",
        make_target="validate-raw",
        dry_run=args.dry_run,
        jobs=args.jobs,
    )

    payload = {
        "config": {
            "make_bin": make_bin,
            "repo_root": str(repo_root),
            "jobs": args.jobs,
            "include_vscode": include_vscode,
            "dry_run": args.dry_run,
            "targets": target_list,
            "method": "isolated_cold_worktree_with_full_worktree_setup",
            "profiled_ref": "HEAD (detached worktrees; uncommitted changes excluded)",
        },
        "target_runs": [asdict(result) for result in target_results],
        "validate_raw_run": asdict(validate_raw_result),
        "summary": emit_summary(
            target_results=target_results,
            validate_raw_result=validate_raw_result,
            jobs=args.jobs,
        ),
    }

    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"report: {output_path}")

    failures = collect_failures(target_results + [validate_raw_result])
    if failures:
        # Return the first failing code for shell compatibility.
        return failures[0].exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
