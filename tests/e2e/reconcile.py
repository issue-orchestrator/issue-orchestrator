"""Remove leftover e2e PRs from the test repository, on an operator's word (#9523).

Nothing a run can observe proves that another run has ended: a full live
suite, a delayed dependency or a stuck process can outlive any fixed age. So
the live suite only ever removes the PRs of issues it created itself, and
reports the rest. An operator who knows no run is live against the test
repository says so here, naming it, and only then is every PR carrying the e2e
artifact label removed, with its branch:

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
    IdleRepositoryScope,
)


def reconcile(confirmed_idle_repo: str, *, github: CleanupGitHub | None = None) -> int:
    """Remove every e2e-labelled PR from ``confirmed_idle_repo``; the number closed."""
    cleanup = E2EArtifactCleanup.for_test_repo(confirmed_idle_repo, github=github)
    return cleanup.sweep(IdleRepositoryScope(repo=cleanup.repo))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.reconcile",
        description="Remove leftover e2e PRs (and their branches) from the e2e test repository.",
    )
    parser.add_argument(
        "--no-live-run-against",
        required=True,
        metavar="OWNER/REPO",
        help="Confirm that no e2e run is live against this repository (must be E2E_TEST_REPO).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    closed = reconcile(args.no_live_run_against)
    print(f"closed {closed} leftover e2e PR(s) in {args.no_live_run_against}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
