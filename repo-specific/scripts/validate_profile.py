#!/usr/bin/env python3
"""Profile validate target execution and report bottlenecks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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


def detect_jobs() -> int:
    cpu_count = os.cpu_count()
    if cpu_count is None or cpu_count <= 0:
        return 5
    return cpu_count


def run_command(name: str, command: list[str], dry_run: bool) -> CommandResult:
    print(f"[profile] {name}: {' '.join(command)}")
    if dry_run:
        return CommandResult(name=name, command=command, wall_seconds=0.0, exit_code=0)

    start = time.monotonic()
    completed = subprocess.run(command, check=False)
    wall = time.monotonic() - start
    return CommandResult(
        name=name,
        command=command,
        wall_seconds=wall,
        exit_code=completed.returncode,
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
            "(default: .issue-orchestrator/diagnostics/validate-profile-<timestamp>.json)"
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
    return parser.parse_args()


def default_output_path() -> Path:
    ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".issue-orchestrator/diagnostics") / f"validate-profile-{ts}.json"


def fail_on_nonzero(results: list[CommandResult]) -> int:
    for result in results:
        if result.exit_code != 0:
            print(
                f"[profile] command failed: {result.name} (exit={result.exit_code})",
                file=sys.stderr,
            )
            return result.exit_code
    return 0


def emit_summary(
    target_results: list[CommandResult],
    parallel_results: list[CommandResult],
    include_vscode: bool,
    jobs: int,
) -> dict[str, object]:
    serial_total = sum(result.wall_seconds for result in target_results)
    observed_parallel_total = sum(result.wall_seconds for result in parallel_results)
    max_parallel_target = max((result.wall_seconds for result in target_results), default=0.0)
    vs_time = 0.0
    if include_vscode and target_results:
        for result in target_results:
            if result.name.endswith("test-vscode"):
                vs_time = result.wall_seconds
                break
    estimated_critical_path = max_parallel_target + vs_time

    sorted_targets = sorted(target_results, key=lambda r: r.wall_seconds, reverse=True)
    top_targets = sorted_targets[:3]

    summary = {
        "timestamp_utc": datetime.now(tz=UTC).isoformat(),
        "jobs": jobs,
        "serial_target_sum_seconds": serial_total,
        "observed_parallel_seconds": observed_parallel_total,
        "estimated_critical_path_seconds": estimated_critical_path,
        "parallel_speedup_vs_serial": (
            (serial_total / observed_parallel_total) if observed_parallel_total > 0 else None
        ),
        "top_targets": [asdict(result) for result in top_targets],
    }

    print()
    print("Validate Profile Summary")
    print("------------------------")
    print(f"jobs: {jobs}")
    print(f"serial target sum: {serial_total:.2f}s")
    print(f"observed parallel total: {observed_parallel_total:.2f}s")
    print(f"estimated critical path: {estimated_critical_path:.2f}s")
    if summary["parallel_speedup_vs_serial"] is not None:
        print(f"speedup vs serial: {summary['parallel_speedup_vs_serial']:.2f}x")
    print("top targets:")
    for result in top_targets:
        print(f"  - {result.name}: {result.wall_seconds:.2f}s")
    return summary


def main() -> int:
    args = parse_args()
    make_bin = args.make_bin
    include_vscode = not args.no_vscode
    output_path = args.output or default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.targets:
        target_list = [target.strip() for target in args.targets.split(",") if target.strip()]
    else:
        target_list = discover_validate_targets(make_bin)
    if include_vscode:
        target_list.append("test-vscode")

    target_results: list[CommandResult] = []
    for target in target_list:
        target_results.append(
            run_command(
                name=f"target:{target}",
                command=[make_bin, "--output-sync=target", target],
                dry_run=args.dry_run,
            ),
        )

    parallel_results: list[CommandResult] = []
    parallel_results.append(
        run_command(
            name="parallel:_validate-impl",
            command=[
                make_bin,
                f"-j{args.jobs}",
                "--output-sync=target",
                "_validate-impl",
            ],
            dry_run=args.dry_run,
        ),
    )
    if include_vscode:
        parallel_results.append(
            run_command(
                name="parallel:test-vscode",
                command=[make_bin, "--output-sync=target", "test-vscode"],
                dry_run=args.dry_run,
            ),
        )

    payload = {
        "config": {
            "make_bin": make_bin,
            "jobs": args.jobs,
            "include_vscode": include_vscode,
            "dry_run": args.dry_run,
            "targets": target_list,
        },
        "target_runs": [asdict(result) for result in target_results],
        "parallel_runs": [asdict(result) for result in parallel_results],
        "summary": emit_summary(
            target_results=target_results,
            parallel_results=parallel_results,
            include_vscode=include_vscode,
            jobs=args.jobs,
        ),
    }

    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"report: {output_path}")

    exit_code = fail_on_nonzero(target_results)
    if exit_code != 0:
        return exit_code
    return fail_on_nonzero(parallel_results)


if __name__ == "__main__":
    raise SystemExit(main())
