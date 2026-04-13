#!/usr/bin/env python3
"""Run the applied-AI portfolio benchmark and emit a reusable artifact bundle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from typing import Sequence

from issue_orchestrator.testing.support.portfolio_benchmark import (
    BENCHMARK_TEST_FILE,
    DEFAULT_OUTPUT_DIR,
    PortfolioBenchmarkReport,
    build_pytest_command,
    list_cases,
    parse_junit_report,
    select_cases,
    write_report_artifacts,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the applied-AI portfolio benchmark against deterministic simulated scenarios.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing tests/simulated_scenarios/.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where summary artifacts should be written.",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        default=[],
        help="Benchmark case id to run. Repeat to select multiple cases.",
    )
    parser.add_argument(
        "--pytest-arg",
        action="append",
        dest="pytest_args",
        default=[],
        help="Extra argument passed through to pytest. Repeat as needed.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available benchmark cases and exit.",
    )
    return parser


def run_portfolio_benchmark(
    *,
    repo_root: Path,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    case_ids: Sequence[str] | None = None,
    extra_pytest_args: Sequence[str] | None = None,
) -> PortfolioBenchmarkReport:
    benchmark_cases = select_cases(case_ids)
    if not benchmark_cases:
        raise ValueError("At least one benchmark case is required.")

    repo_root = repo_root.resolve()
    if not (repo_root / BENCHMARK_TEST_FILE).exists():
        raise FileNotFoundError(
            f"Portfolio benchmark expects {BENCHMARK_TEST_FILE.as_posix()} under {repo_root}"
        )

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    junit_xml_path = output_dir / "junit.xml"
    command = build_pytest_command(
        junit_xml_path=junit_xml_path,
        cases=benchmark_cases,
        extra_pytest_args=extra_pytest_args,
    )

    completed = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    report = PortfolioBenchmarkReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        repo_root=repo_root,
        output_dir=output_dir,
        command=command,
        pytest_exit_code=completed.returncode,
        results=tuple(parse_junit_report(junit_xml_path, benchmark_cases)),
    )
    write_report_artifacts(
        report=report,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    if args.list:
        print(list_cases(), end="")
        return 0

    try:
        report = run_portfolio_benchmark(
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            case_ids=args.case_ids,
            extra_pytest_args=args.pytest_args,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"Portfolio benchmark {report.overall_status}: {report.counts}")
    print(f"Artifacts written to {report.output_dir}")
    return 0 if report.overall_status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
