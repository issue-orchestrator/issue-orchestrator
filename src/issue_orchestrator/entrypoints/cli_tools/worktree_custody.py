#!/usr/bin/env python3
"""Hold a checkout, or let it go, from the command line (#7274).

The operator half of custody. Every removal path already refuses a held
checkout; this is how a person says "that one is mine" and, afterwards, "I have
what I needed".

    issue-orchestrator worktree-custody list
    issue-orchestrator worktree-custody hold <path> --reason "salvaging #6410"
    issue-orchestrator worktree-custody release <path> --reason "collected"
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from ...execution.worktree_adapter import GitWorktreeManager
from ...ports.worktree_custody import (
    CustodyError,
    CustodyGrant,
    CustodyRelease,
)


def _holder(requested: str | None) -> str:
    """Who is taking or ending the grant.

    Defaults to the login name rather than a placeholder: an audit trail whose
    every row says "operator" answers nothing six months later.
    """
    if requested:
        return requested
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _render(grant: CustodyGrant) -> str:
    branch = grant.branch or "(detached)"
    return (
        f"{grant.path}\n"
        f"  branch: {branch}\n"
        f"  holder: {grant.holder}\n"
        f"  since:  {grant.taken_at.isoformat()}\n"
        f"  reason: {grant.reason}"
    )


def cmd_list(manager: GitWorktreeManager, args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root or ".").resolve()
    held = manager.checkouts_in_custody(repo_root)
    breached = manager.breached_custody(repo_root)
    if not held:
        print("No checkouts are in custody.")
    for grant in held:
        if grant not in breached:
            print(_render(grant))
    if breached:
        # Custody prevents inside this codebase and detects outside it. Saying
        # nothing here would leave an operator believing a promise that was
        # already broken.
        print("\nGONE despite being held -- something outside removed these:")
        for grant in breached:
            print(_render(grant))
        return 1
    return 0


def cmd_hold(manager: GitWorktreeManager, args: argparse.Namespace) -> int:
    path = Path(args.path).resolve()
    holder = _holder(args.holder)
    grant = manager.take_custody(path, holder=holder, reason=args.reason)
    if grant.holder != holder:
        # Someone already holds it, and the first holder keeps it. Say who,
        # rather than reporting a success that took nothing.
        print(f"Already held by someone else:\n{_render(grant)}", file=sys.stderr)
        return 1
    print(f"Held. No removal path will discard this checkout.\n{_render(grant)}")
    return 0


def cmd_release(manager: GitWorktreeManager, args: argparse.Namespace) -> int:
    path = Path(args.path).resolve()
    released = manager.release_custody(
        path, CustodyRelease(holder=_holder(args.holder), reason=args.reason)
    )
    if released is None:
        print(f"{path} was not in custody.", file=sys.stderr)
        return 1
    print(f"Released. Ordinary cleanup may now remove it.\n{_render(released)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue-orchestrator worktree-custody", description=__doc__
    )
    sub = parser.add_subparsers(dest="action", required=True)

    listing = sub.add_parser("list", help="Show every checkout in custody")
    listing.add_argument(
        "--repo-root", default=None, help="Repository to list (default: cwd)"
    )
    listing.set_defaults(run=cmd_list)

    hold = sub.add_parser("hold", help="Protect a checkout from every removal path")
    hold.add_argument("path", help="The worktree to hold")
    hold.add_argument("--reason", required=True, help="Why it is being held")
    hold.add_argument("--holder", default=None, help="Who holds it (default: you)")
    hold.set_defaults(run=cmd_hold)

    release = sub.add_parser("release", help="End a grant without removing anything")
    release.add_argument("path", help="The worktree to release")
    release.add_argument("--reason", required=True, help="Why it is being released")
    release.add_argument("--holder", default=None, help="Who is releasing it")
    release.set_defaults(run=cmd_release)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(GitWorktreeManager(), args)
    except (CustodyError, ValueError, OSError) as exc:
        # Every way custody can refuse arrives here as a message and an exit
        # status. A traceback tells an operator nothing they can act on, and
        # this command is the one they reach for when work is at stake.
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
