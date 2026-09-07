"""Inspect budgeted coverage or explicitly request a configured suite run."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

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
    if not suites:
        print("No budgeted validation suites configured.")
        return 0
    cycle, store = build_budgeted_validation_cycle(root)
    acquired = True
    if args.command != "status":
        acquired = cycle.run(tuple(suites.values()), force=args.command == "run")
    outcomes = set()
    for suite in suites.values():
        history = store.read(suite)
        print(json.dumps({"suite": suite.name, "coverage_outcome": history.coverage_outcome,
                          **asdict(history)}, default=str, indent=2))
        if suite.enabled:
            outcomes.add(history.coverage_outcome)
    return coverage_exit_code(outcomes, inspect_only=args.command == "status", acquired=acquired)


if __name__ == "__main__":
    raise SystemExit(main())
