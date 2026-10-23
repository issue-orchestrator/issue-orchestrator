"""Remove leftover e2e artifacts from the test repository, on an operator's word (#9523).

Nothing a run can observe proves that another run has ended: a full live
suite, a delayed dependency or a stuck process can outlive any fixed age. So
the live suite only ever closes the PRs and issues it recorded creating,
reports the rest, and deletes no label. An operator who knows no run is live
against the test repository says so here, naming it, and only then are every
PR and open issue carrying an e2e marker (each PR with its branch), and every
per-test and per-run e2e label removed. A label goes only once nothing open
carries it: it is how the next run of this command finds what still does, so
an unfinished run exits non-zero and keeps the labels for the retry:

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

from tests.e2e.fixtures.artifact_cleanup import (
    CleanupGitHub,
    E2EArtifactCleanup,
    IdleReconciliation,
    IdleRepositoryScope,
)


def reconcile(confirmed_idle_repo: str, *, github: CleanupGitHub | None = None) -> IdleReconciliation:
    """Remove every e2e artifact from ``confirmed_idle_repo`` (the cleanup
    owner's :meth:`~E2EArtifactCleanup.reconcile_idle`, which keeps each label
    until nothing open depends on it)."""
    cleanup = E2EArtifactCleanup.for_test_repo(confirmed_idle_repo, github=github)
    return cleanup.reconcile_idle(
        IdleRepositoryScope(repo=cleanup.repo),
        comment="Closed by e2e reconciliation (no run live).",
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
        f"{done.issues_closed} issue(s), deleted {len(done.labels_deleted)} label(s)",
        file=sys.stderr,
    )
    if done.complete:
        return 0
    for failure in done.failures:
        print(f"  not finished: {failure.what}: {failure.reason}", file=sys.stderr)
    print(
        f"  kept {len(done.labels_retained)} label(s) so the next run of this command "
        "can still find what carries them. Fix the cause and run it again.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
