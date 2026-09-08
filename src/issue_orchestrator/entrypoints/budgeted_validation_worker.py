"""Execute one immutable budgeted validation request from the engine."""

import argparse
import json
from pathlib import Path
import signal

from ..infra.budgeted_validation_config import parse_budgeted_validation
from .bootstrap import build_budgeted_validation_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    payload = json.loads(args.request.read_text())
    suites = tuple(parse_budgeted_validation(payload["suites"]).values())
    cycle, _ = build_budgeted_validation_cycle(Path(payload["repo_root"]))
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        cycle.run(suites)
    finally:
        args.request.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
