"""Fault-injecting agent shim for the tech-lead exam (#7304).

One script plays every scripted role; its role and its planted fault are baked
into each agent's configured command, so a case says exactly which launch
misbehaves and how:

* ``--role coder`` — an initial coding session commits a change and completes
  through ``coding-done``. Inside a review exchange (the engine sets
  ``ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE``) it only waits on its prompt
  stream and never commits, so the validated HEAD stays the published one.
* ``--role reviewer`` — a post-publish code review approves through
  ``reviewer-done``. Inside a review exchange it applies ``--exchange-fault``:
  ``exit-silently`` exits without answering, which the engine records as a
  reviewer no-completion (the porchpin 09-23 shape, minus the dialog).

``--hold-until PATH`` makes a session wait, before doing anything, until
PATH exists. The upgrade case (Case U) uses it to keep work mid-flight across
an engine stop: the harness creates PATH only after the candidate engine has
taken over.

Completion commands come from the ENGINE's ``scripts/`` directory, which the
engine prepends to every agent's PATH, so a run against an older engine uses
that engine's own completion contract.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

RESPONSE_FILE_ENV = "ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"
EXCHANGE_FAULTS = ("none", "exit-silently")


def log(message: str) -> None:
    print(f"[exam-agent {time.strftime('%H:%M:%S')} pid={os.getpid()}] {message}", flush=True)


def run(argv: list[str]) -> None:
    log(f"$ {' '.join(argv)}")
    subprocess.run(argv, check=True)


def initial_coding_session() -> None:
    marker = Path("exam-output.txt")
    marker.write_text(f"tech-lead exam work item, written {time.ctime()}\n", encoding="utf-8")
    run(["git", "add", str(marker)])
    run(
        [
            "git",
            "commit",
            "--signoff",
            "-m",
            "Exam: planted work item",
            "--trailer",
            "Agent-Status: completed",
            "--trailer",
            "Agent-Implementation: exam work item",
            "--trailer",
            "Agent-Problems: None",
        ]
    )
    argv = [
        "coding-done",
        "completed",
        "--implementation",
        "Exam work item committed",
        "--problems",
        "None",
    ]
    labels = [label for label in os.environ.get("E2E_PR_LABELS", "").split(",") if label]
    if labels:
        argv += ["--pr-labels", *labels]
    run(argv)


def idle_on_prompts() -> None:
    """Hold the exchange coder open without ever changing the worktree."""
    log("exchange coder: waiting on prompts, never committing")
    for line in sys.stdin:
        log(f"exchange coder ignored prompt line ({len(line)} chars)")


def approve_review() -> None:
    run(
        [
            "reviewer-done",
            "approved",
            "--summary",
            "Exam reviewer: approved",
            "--risk",
            "low",
        ]
    )


def hold_until(release: Path) -> None:
    """Stay mid-flight until the harness releases the work."""
    log(f"holding until {release} exists")
    while not release.exists():
        time.sleep(2)
    log("released")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("coder", "reviewer"), required=True)
    parser.add_argument("--exchange-fault", choices=EXCHANGE_FAULTS, default="none")
    parser.add_argument("--hold-until", type=Path, default=None)
    args = parser.parse_args()
    in_exchange = bool(os.environ.get(RESPONSE_FILE_ENV))
    log(f"role={args.role} in_exchange={in_exchange} fault={args.exchange_fault}")
    if args.hold_until is not None:
        hold_until(args.hold_until)

    if args.role == "coder":
        if in_exchange:
            idle_on_prompts()
        else:
            initial_coding_session()
        return 0

    if not in_exchange:
        approve_review()
        return 0
    if args.exchange_fault == "exit-silently":
        log("exchange reviewer: planted fault, exiting without a verdict")
        return 0
    raise SystemExit(
        "exam reviewer launched inside a review exchange without a planted fault;"
        " this shim only answers post-publish reviews"
    )


if __name__ == "__main__":
    sys.exit(main())
