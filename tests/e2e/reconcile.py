"""Remove leftover e2e artifacts from the test repository, on an operator's word (#9523).

Nothing a run can observe proves that another run has ended: a full live
suite, a delayed dependency or a stuck process can outlive any fixed age. So
the live suite only ever closes the PRs and issues it recorded creating,
reports the rest, and deletes no label. An operator who knows no run is live
against the test repository says so here, naming it, and only then are every
PR carrying the e2e artifact label (with its branch), every open issue
carrying an e2e marker, and every per-test and per-run e2e label removed:

    IO_E2E=1 E2E_TEST_REPO=owner/test-repo \\
        python -m tests.e2e.reconcile --no-live-run-against owner/test-repo

The confirmation must name ``E2E_TEST_REPO``, which must not be this
checkout's own repository, and agent sessions are refused (the cleanup owner's
checks).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from tests.e2e.fixtures.artifact_cleanup import (
    E2E_ARTIFACT_LABEL,
    CleanupGitHub,
    E2EArtifactCleanup,
    IdleRepositoryScope,
)


@dataclass(frozen=True)
class Reconciled:
    prs_closed: int
    issues_closed: int
    labels_deleted: int


def reconcile(confirmed_idle_repo: str, *, github: CleanupGitHub | None = None) -> Reconciled:
    """Remove every e2e artifact from ``confirmed_idle_repo``: PRs and their
    branches first, then issues, then the labels nothing needs any more."""
    cleanup = E2EArtifactCleanup.for_test_repo(confirmed_idle_repo, github=github)
    idle = IdleRepositoryScope(repo=cleanup.repo)
    prs_closed = cleanup.sweep(idle)
    # Issues the engine filed may carry only a per-run label, not the static one.
    issue_labels = [E2E_ARTIFACT_LABEL] + [
        name for name in cleanup.label_names() if idle.admits_label(name, cleanup.repo)
    ]
    issues_closed = cleanup.sweep_issues(
        idle, comment="Closed by e2e reconciliation (no run live).", labels=issue_labels
    )
    return Reconciled(
        prs_closed=prs_closed,
        issues_closed=issues_closed,
        labels_deleted=cleanup.sweep_labels(idle),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.reconcile",
        description="Remove leftover e2e PRs, branches, issues and labels from the e2e test repository.",
    )
    parser.add_argument(
        "--no-live-run-against",
        required=True,
        metavar="OWNER/REPO",
        help="Confirm that no e2e run is live against this repository (must be E2E_TEST_REPO).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    done = reconcile(args.no_live_run_against)
    print(
        f"{args.no_live_run_against}: closed {done.prs_closed} PR(s) and "
        f"{done.issues_closed} issue(s), deleted {done.labels_deleted} label(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
