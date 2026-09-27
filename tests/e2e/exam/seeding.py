"""Plant exam state on GitHub before (or while) the engine runs.

Seeded artifacts carry ``io-e2e-test-data`` (so a real engine's
``filtering.exclude_labels`` never touches them and the e2e reconciliation
cleans them up) plus the run's own label (so only this run's engine sees
them). A seeded PR is shaped like one the engine published: an
``<issue>-<slug>`` branch, a ``Closes #<issue>`` body with the orchestrator
marker, and the code-review label.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable
from pathlib import Path

from issue_orchestrator.domain.models import ORCHESTRATOR_PR_MARKER
from issue_orchestrator.ports.pull_request_tracker import StatusCheckRollupRead

from tests.e2e.fixtures import _github_adapter

logger = logging.getLogger(__name__)

E2E_DATA_LABEL = "io-e2e-test-data"


@dataclass(frozen=True)
class SeededPullRequest:
    number: int
    branch: str
    head_sha: str


def _git(cwd: Path, *argv: str, env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(argv)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def seed_pull_request(
    *,
    repo: str,
    repo_root: Path,
    issue_number: int,
    slug: str,
    labels: list[str],
    draft: bool,
    register_branch: Callable[[str], None],
) -> SeededPullRequest:
    """Push a one-file commit on top of origin/main and open a PR for it.

    Built with plumbing in a private index so no checkout is touched. The file
    is outside ``src/`` so CI's change filter skips the heavy lanes and the PR
    goes green in seconds, like the porchpin PRs it stands for.
    """
    _git(repo_root, "fetch", "--quiet", "origin", "main")
    base = _git(repo_root, "rev-parse", "origin/main")
    branch = f"{issue_number}-{slug}"
    content = f"tech-lead exam seed for #{issue_number} at {time.ctime()}\n"
    blob = _git(repo_root, "hash-object", "-w", "--stdin", stdin=content)
    with tempfile.TemporaryDirectory(prefix="exam-index-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo_root, "read-tree", base, env=env)
        _git(
            repo_root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},exam-output.txt",
            env=env,
        )
        tree = _git(repo_root, "write-tree", env=env)
    author = _git(repo_root, "var", "GIT_AUTHOR_IDENT").rsplit(" ", 2)[0]
    message = f"Exam seed for #{issue_number}\n\nSigned-off-by: {author}\n"
    commit = _git(repo_root, "commit-tree", tree, "-p", base, stdin=message)
    # The engine pushes e2e branches with hooks skipped (E2E_SKIP_PUSH_HOOKS);
    # a seeded test-data branch is pushed the same way.
    # Register BEFORE pushing: a push that lands but reports failure, or a
    # create_pr that fails after it, must still leave cleanup the branch.
    register_branch(branch)
    _git(repo_root, "push", "--no-verify", "--quiet", "origin", f"{commit}:refs/heads/{branch}")
    adapter = _github_adapter(repo)
    pr = adapter.create_pr(
        title=f"#{issue_number}: exam seed",
        body=f"Closes #{issue_number}\n\n{ORCHESTRATOR_PR_MARKER}\n",
        head=branch,
        draft=draft,
    )
    for label in labels:
        adapter.add_label(pr.number, label)
    logger.info("[EXAM] seeded PR #%d on %s (%s)", pr.number, branch, commit[:10])
    return SeededPullRequest(number=pr.number, branch=branch, head_sha=commit)


def wait_for_checks(repo: str, pr_number: int, *, timeout_s: float) -> str:
    """Wait until the PR's checks are conclusive; return GitHub's rollup state."""
    adapter = _github_adapter(repo)
    deadline = time.monotonic() + timeout_s
    state = "UNREAD"
    while time.monotonic() < deadline:
        read = adapter.read_pr_status_check_rollup(pr_number)
        state = describe_rollup(read)
        if state in {"SUCCESS", "FAILURE", "ERROR", "NONE", "PERMISSION_DENIED"}:
            return state
        time.sleep(20)
    return state


def describe_rollup(read: StatusCheckRollupRead) -> str:
    """A rollup read as one word: GitHub's state, ``NONE``, or why it is unknown."""
    if read.capability != "ok":
        return read.capability.upper()
    return read.state or "NONE"
