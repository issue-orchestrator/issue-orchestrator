#!/usr/bin/env python3
"""Crude bisection runner for e2e tests.

Collects e2e nodeids, runs halves, and narrows to failing subset.
Writes progress to /tmp/e2e-orchestrator-logs/e2e-bisect.log.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable


LOG_PATH = Path("/tmp/e2e-orchestrator-logs/e2e-bisect.log")


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as handle:
        handle.write(msg + "\n")


def collect_tests(targets: list[str]) -> list[str]:
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", *targets]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return [line.strip() for line in result.stdout.splitlines() if "::" in line]


def run_tests(nodeids: Iterable[str], pytest_args: list[str]) -> int:
    nodeids_list = list(nodeids)
    if not nodeids_list:
        return 0
    cmd = [sys.executable, "-m", "pytest", *pytest_args, *nodeids_list]
    log(f"RUN {len(nodeids_list)} tests: {' '.join(nodeids_list)}")
    return subprocess.call(cmd)


def bisect_tests(tests: list[str], pytest_args: list[str]) -> None:
    current = tests
    step = 0
    while len(current) > 1:
        step += 1
        mid = len(current) // 2
        left = current[:mid]
        right = current[mid:]
        log(f"STEP {step}: left={len(left)} right={len(right)}")

        left_code = run_tests(left, pytest_args)
        if left_code != 0:
            current = left
            log(f"STEP {step}: left failed")
            continue

        right_code = run_tests(right, pytest_args)
        if right_code != 0:
            current = right
            log(f"STEP {step}: right failed")
            continue

        log(f"STEP {step}: both halves passed; no failing tests detected")
        return

    if current:
        log(f"FAILURE ISOLATED: {current[0]}")
        run_tests(current, pytest_args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bisect failing e2e tests.")
    parser.add_argument("targets", nargs="*", default=["tests/e2e"], help="pytest targets")
    parser.add_argument("--filter", dest="filter", default="", help="substring filter for nodeids")
    parser.add_argument("--pytest-args", dest="pytest_args", default="-v -s", help="extra pytest args")
    args = parser.parse_args()

    if "E2E_KEEP_ARTIFACTS" not in os.environ:
        os.environ["E2E_KEEP_ARTIFACTS"] = "1"

    pytest_args = args.pytest_args.split()
    tests = collect_tests(args.targets)
    if args.filter:
        tests = [t for t in tests if args.filter in t]
    if not tests:
        print("No tests matched.")
        return 1

    log(f"COLLECTED {len(tests)} tests")
    bisect_tests(tests, pytest_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
