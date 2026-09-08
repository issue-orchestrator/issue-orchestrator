"""Inspect budgeted coverage or explicitly request a configured suite run."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from ...control.budgeted_validation_coverage import BudgetedValidationCoverageOwner
from ...domain.budgeted_validation import coverage_exit_code
from ...infra.budgeted_validation_config import parse_budgeted_validation
from ...infra.validation_config_loader import load_runtime_validation_config, load_validation_config_from_file
from ..bootstrap import build_budgeted_validation_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "check", "run"))
    parser.add_argument("--config", type=Path, help="IO YAML config; otherwise use the active runtime selection")
    parser.add_argument("--suite", help="Restrict the request to one configured suite")
    args = parser.parse_args()
    root = Path.cwd()
    data = load_validation_config_from_file(args.config) if args.config else load_runtime_validation_config(root)
    suites = parse_budgeted_validation(data.get("budgeted", {}))
    if args.suite:
        if args.suite not in suites:
            parser.error(f"Unknown budgeted suite {args.suite!r}")
        suites = {args.suite: suites[args.suite]}
    cycle, store = build_budgeted_validation_cycle(root)
    acquired = True
    if args.command != "status":
        acquired = cycle.run(tuple(suites.values()), force=args.command == "run")
    configured = tuple(suites.values())
    coverage = BudgetedValidationCoverageOwner(store).snapshot(
        configured, retained_name=args.suite,
    )
    if not coverage.entries:
        print("No budgeted validation suites configured or retained.")
        return 0
    for entry in coverage.entries:
        suite, history = entry.suite, entry.history
        print(json.dumps({"suite": suite.name, "coverage_outcome": history.coverage_outcome,
                          **asdict(history)}, default=str, indent=2))
    return coverage_exit_code(
        set(coverage.outcomes),
        inspect_only=args.command == "status",
        acquired=acquired,
    )


if __name__ == "__main__":
    raise SystemExit(main())
