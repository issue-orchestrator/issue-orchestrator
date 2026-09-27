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
import time
from dataclasses import dataclass
from pathlib import Path

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

    @classmethod
    def create(
        cls, *, harness_root: Path, ref: str, case_id: str, parent: Path = WORKTREE_PARENT
    ) -> "EngineCheckout":
        commit = _git(harness_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
        origin = _git(harness_root, "remote", "get-url", "origin")
        venv = harness_root / ".venv"
        if not venv.is_dir():
            raise RuntimeError(f"harness virtualenv missing at {venv}")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root = parent / f"exam-engine-{commit[:10]}-{case_id[:1].lower()}-{stamp}"
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
        except BaseException:
            checkout.remove()
            raise
        logger.info("[EXAM] engine checkout %s at %s", root, commit)
        return checkout

    @property
    def state_dir(self) -> Path:
        return self.root / ".issue-orchestrator" / "state"

    def remove(self) -> None:
        # The venv is a symlink to the HARNESS's; unlink it so rmtree can
        # never follow it into the harness.
        link = self.root / ".venv"
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(self.root)
