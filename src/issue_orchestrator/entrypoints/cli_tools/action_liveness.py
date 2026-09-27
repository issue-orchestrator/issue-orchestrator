#!/usr/bin/env python3
"""See, and release, what the orchestrator stopped retrying (#7350).

The operator half of the action liveness owner. Most parks name an issue and
are released by Retry or Dismiss on it; a park that names none (an ``engine``
subject, such as authoring a new tech-lead anchor) is released here, and any
park can be.

    issue-orchestrator action-liveness list
    issue-orchestrator action-liveness release --subject engine --action create_tech_lead_issue

Works on the engine's durable store directly, so it answers while the engine is
running or stopped. A released block that was escalated is owed its withdrawal,
and its ``action.released`` announcement, which the running engine settles
and publishes on its next planning cycle.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ...control.action_liveness import release_parked_action
from ...domain.action_liveness import ActionIdentity
from ..bootstrap_action_liveness import ACTION_LIVENESS_DB
from ...execution.action_liveness_store import SQLiteActionLivenessStore
from ...infra.repo_identity import state_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="issue-orchestrator action-liveness")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Every parked action")
    release = commands.add_parser("release", help="Give one parked action a fresh budget")
    release.add_argument("--subject", required=True, help="e.g. engine, issue:410")
    release.add_argument("--action", required=True, help="e.g. create_tech_lead_issue")
    for command in (listing, release):
        command.add_argument("--repo-root", default=".", help="Repository the engine runs for")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteActionLivenessStore(
        state_dir(Path(args.repo_root).resolve()) / ACTION_LIVENESS_DB
    )
    if args.command == "list":
        for row in store.parked_rows():
            print(
                f"{row.key.identity.subject}\t{row.key.identity.action}"
                f"\t{row.last_outcome.value}\t{row.last_reason}"
            )
        return 0
    released = release_parked_action(store, ActionIdentity(args.subject, args.action))
    print(f"released {len(released)} row(s) for {args.action} on {args.subject}")
    return 0 if released else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
