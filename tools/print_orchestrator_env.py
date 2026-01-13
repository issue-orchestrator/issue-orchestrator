#!/usr/bin/env python3
"""Print orchestrator env vars for CI/testing.

Usage:
    # Shell exports (for sourcing)
    python tools/print_orchestrator_env.py --format=shell

    # GitHub Actions format (for $GITHUB_ENV)
    python tools/print_orchestrator_env.py --format=github

    # JSON format
    python tools/print_orchestrator_env.py --format=json
"""

import argparse
import json
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from issue_orchestrator.domain.env_vars import get_test_env_dict


def main() -> None:
    parser = argparse.ArgumentParser(description="Print orchestrator env vars")
    parser.add_argument(
        "--format",
        choices=["shell", "github", "json"],
        default="shell",
        help="Output format",
    )
    args = parser.parse_args()

    env_vars = get_test_env_dict()

    if args.format == "shell":
        # Shell export format
        for name, value in env_vars.items():
            print(f"export {name}='{value}'")
    elif args.format == "github":
        # GitHub Actions format (append to $GITHUB_ENV)
        for name, value in env_vars.items():
            print(f"{name}={value}")
    elif args.format == "json":
        print(json.dumps(env_vars, indent=2))


if __name__ == "__main__":
    main()
