#!/usr/bin/env python3
"""Run the applied-AI portfolio benchmark and emit a reusable artifact bundle."""

from __future__ import annotations

import sys

from issue_orchestrator.testing.support.portfolio_benchmark import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
