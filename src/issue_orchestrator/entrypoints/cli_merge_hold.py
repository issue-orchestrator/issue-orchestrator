"""``orchestrator hold-merge-for-human``: move an agent's PR question to a merge hold (#7678).

A one-shot operator command for state written before #7678 typed the request:
an agent's ``pr_labels: [needs-human]`` that landed on its ISSUE as a work
block. See :mod:`..control.merge_hold_migration`. **Dry-run by default**; it
holds the repo lock, so the engine must be stopped while it runs.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from rich.console import Console

from ..control.merge_hold_migration import MergeHoldMigration, MergeHoldMoveStatus

console = Console()

_EXIT = {
    MergeHoldMoveStatus.MOVED: 0,
    MergeHoldMoveStatus.WOULD_MOVE: 0,
    MergeHoldMoveStatus.REFUSED: 2,
    MergeHoldMoveStatus.FAILED: 1,
}


def cmd_hold_merge_for_human(args: argparse.Namespace) -> int:
    from ..control.review_scope import extract_issue_number_from_pr
    from ..infra.repo_scope import require_repo
    from ..infra.repo_lock import AlreadyRunning, held_repo_lock
    from .cli_support import load_config
    from .cli_tech_lead import _build_orchestrator, _release, _report_lock_conflict

    config = load_config(args)
    try:
        with held_repo_lock(config.repo_root):
            orchestrator = _build_orchestrator(config)
            try:
                deps = orchestrator.deps
                repo = require_repo(config)
                migration = MergeHoldMigration(
                    block=deps.needs_human_block,
                    labels=deps.label_manager,
                    read_issue=deps.repository_host.get_issue,
                    read_pr=deps.repository_host.get_pr,
                    pr_issue_number=lambda pr: extract_issue_number_from_pr(pr, repo_slug=repo),
                )
                outcome = migration.move(args.issue, args.pr, apply=bool(args.apply))
            finally:
                _release(orchestrator)
    except AlreadyRunning as exc:
        _report_lock_conflict(exc, command="hold-merge-for-human")
        return 1
    console.print(f"[bold]{outcome.status.value}[/bold]: {outcome.detail}")
    return _EXIT[outcome.status]


def add_hold_merge_parser(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    handler: "Callable[[argparse.Namespace], int]",
) -> None:
    parser = subparsers.add_parser(
        "hold-merge-for-human",
        help=(
            "Move an agent's question about its PR from the issue's work block to the"
            " PR's merge hold (#7678; dry-run by default)"
        ),
    )
    parser.add_argument("--issue", type=int, required=True, help="Issue whose needs-human the agent asked for")
    parser.add_argument("--pr", type=int, required=True, help="The issue's open PR the question is about")
    parser.add_argument("--apply", action="store_true", help="Execute the move; without it nothing is written")
    parser.set_defaults(func=handler)
