"""The engine under examination: a fresh checkout of the commit under test.

Every exam run gets its own standalone clone, because the engine keeps its
state under ``<repo_root>/.issue-orchestrator``: running two commits out of
one checkout would hand the older engine the newer one's databases, and
re-running one commit would hand a case the previous case's run history.
The clone reuses the harness's virtualenv (symlinked), and
``OrchestratorProcess`` puts the clone's ``src`` first on PYTHONPATH, so the
engine — and every completion command its agents run — is the commit under
test, while the harness, its shims and its grader stay this tree's.

No e2e imports, so the checkout's failure handling is unit-tested.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tests.e2e.exam.run_identity import RunIdentity, github_remote

logger = logging.getLogger(__name__)

WORKTREE_PARENT = Path.home() / "dev" / "worktree" / "issue-orchestrator"


def _git(cwd: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(argv)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@dataclass(frozen=True)
class EngineCheckout:
    """A fresh, standalone checkout of the commit under test.

    A standalone ``--shared`` clone, not a ``git worktree``: the engine
    refuses to run with its repository inside a linked worktree (the
    validated-work escrow must live outside disposable worktrees). Objects
    are borrowed from the harness's repository, so the clone is cheap; its
    ``origin`` is re-pointed at GitHub so the engine pushes where a real
    engine would.
    """

    root: Path
    commit: str
    #: The local branches the clone held before the engine ran. Every branch
    #: created in the clone after that is the run's own engine's; ``None``
    #: (a checkout ``create`` did not build) proves no branch is the run's.
    baseline_branches: frozenset[str] | None = None

    @classmethod
    def create(
        cls,
        *,
        harness_root: Path,
        ref: str,
        identity: RunIdentity,
        repo: str,
        parent: Path = WORKTREE_PARENT,
    ) -> "EngineCheckout":
        """``repo`` is the run's repository: the clone's ``origin`` points at
        it (``github_remote``), so the engine pushes where the exam looks."""
        commit = _git(harness_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
        origin = github_remote(repo)
        venv = harness_root / ".venv"
        if not venv.is_dir():
            raise RuntimeError(f"harness virtualenv missing at {venv}")
        root = parent / identity.checkout_name(commit)
        # Reserve the destination first: from here on it is ours, and every
        # failure — the clone itself included — removes it (a network outage
        # mid-fetch once left a half-built clone behind).
        root.mkdir(exist_ok=False)
        checkout = cls(root=root, commit=commit)
        try:
            _git(harness_root, "clone", "--quiet", "--shared", "--no-checkout", str(harness_root), str(root))
            _git(root, "remote", "set-url", "origin", origin)
            _git(root, "fetch", "--quiet", "origin", "main")
            # Agent worktrees branch from local ``main`` (ORCHESTRATOR_WORKTREE_BASE_BRANCH).
            _git(root, "branch", "--force", "main", "origin/main")
            _git(root, "checkout", "--quiet", "--detach", commit)
            (root / ".venv").symlink_to(venv)
            checkout = cls(root=root, commit=commit, baseline_branches=_local_branches(root))
        except BaseException:
            checkout._discard()
            raise
        logger.info("[EXAM] engine checkout %s at %s", root, commit)
        return checkout

    def switch_to(self, *, harness_root: Path, ref: str) -> "EngineCheckout":
        """The same checkout, moved to ``ref``: an in-place upgrade.

        Everything the engine keeps beside its code survives the move,
        because it lives outside git's tracked files: the state directory,
        the sqlite stores, and the agent worktrees. The move is refused if
        the base engine left a tracked file dirty.
        """
        commit = _git(harness_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
        _git(self.root, "checkout", "--quiet", "--detach", commit)
        logger.info("[EXAM] engine checkout %s upgraded %s -> %s", self.root, self.commit, commit)
        return EngineCheckout(root=self.root, commit=commit, baseline_branches=self.baseline_branches)

    @property
    def state_dir(self) -> Path:
        return self.root / ".issue-orchestrator" / "state"

    def engine_branches(self) -> list[str]:
        """Branches created in the clone since ``create``: the run's engine's.

        The clone is this run's alone, so nobody else creates branches in it;
        a remote branch of the same name is the one this engine pushed.
        """
        if self.baseline_branches is None or not (self.root / ".git").is_dir():
            return []
        return sorted(_local_branches(self.root) - self.baseline_branches)

    def remove(self, *, register_branch: Callable[[str], None]) -> None:
        """Remove the clone, first registering the branches its engine created.

        Once the clone is gone nothing records which remote branches this
        run's engine pushed, so cleanup could only guess from their names.
        """
        for branch in self.engine_branches():
            register_branch(branch)
        self._discard()

    def _discard(self) -> None:
        self._remove_engine_worktrees()
        # The venv is a symlink to the HARNESS's; unlink it so rmtree can
        # never follow it into the harness.
        link = self.root / ".venv"
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(self.root)

    def _remove_engine_worktrees(self) -> None:
        """Remove every agent worktree the engine created from this clone.

        They live outside the clone (under the engine's worktree base) and
        are registered only in this clone's git metadata, so they are the
        run's own. Once the clone is gone nothing could find them. A clone
        the failed ``create`` never finished has no metadata and no
        worktrees.
        """
        if not (self.root / ".git").is_dir():
            return
        listing = _git(self.root, "worktree", "list", "--porcelain")
        paths = [
            Path(line.removeprefix("worktree "))
            for line in listing.splitlines()
            if line.startswith("worktree ")
        ]
        for path in paths:
            if path.resolve() == self.root.resolve():
                continue
            _git(self.root, "worktree", "remove", "--force", "--force", str(path))
            if path.exists():
                raise RuntimeError(f"engine worktree {path} survived its removal")


def _local_branches(root: Path) -> frozenset[str]:
    listing = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return frozenset(line for line in listing.splitlines() if line)
