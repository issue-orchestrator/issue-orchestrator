"""The remote branches one exam run created, as receipts cleanup can trust (#9523).

A branch name proves nothing: anyone can push ``7-anything``, and a push this
run attempted can fail against a branch that was already there. So the ledger
reads the remote's heads when the run starts, and records a branch only when
it was absent then AND the remote now holds this run's commit. The receipt
keeps that SHA; cleanup deletes the branch only while it is still the head
(``ExamRunScope``). Anything the run cannot prove is left on the remote.

No e2e imports beyond the receipt type, so the ledger is unit-tested.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Mapping

from tests.e2e.exam.run_identity import github_remote
from tests.e2e.fixtures.artifact_cleanup import RemoteBranchReceipt

logger = logging.getLogger(__name__)

#: Reads every branch head of the run's repository: ``{name: sha}``.
HeadsReader = Callable[[], Mapping[str, str]]


class RunBranches:
    """The run's ledger of remote branches it provably created."""

    def __init__(self, read_heads: HeadsReader) -> None:
        self._read_heads = read_heads
        self._before = frozenset(read_heads())
        self._receipts: dict[str, RemoteBranchReceipt] = {}
        self._unique: list[str] = []

    def record(self, branch: str, sha: str) -> bool:
        """Record that this run pushed ``sha`` to ``branch``, if the remote proves it."""
        return self.record_all({branch: sha}) == [branch]

    def record_all(self, pushed: Mapping[str, str]) -> list[str]:
        """Record every ``{branch: sha}`` the remote proves this run created.

        One read of the remote for all of them; returns the branches recorded.
        A branch that existed before the run, or whose remote head is not the
        run's commit (a failed or never-attempted push), is not recorded.
        """
        if not pushed:
            return []
        heads = self._read_heads()
        recorded: list[str] = []
        for branch, sha in pushed.items():
            if branch in self._before:
                logger.warning("[EXAM] %s existed before the run: never the run's to delete", branch)
            elif heads.get(branch) != sha:
                logger.warning("[EXAM] %s does not hold the run's commit %s: not recorded", branch, sha[:10])
            else:
                self._receipts[branch] = RemoteBranchReceipt(branch, sha)
                recorded.append(branch)
        return recorded

    def expect_unique(self, branch: str, run_id: str) -> None:
        """Note ``branch``, named with this run's random id, which something
        other than the harness (the engine, server-side) will create.

        Nobody else can know the name, so at :meth:`receipts` time, a branch
        absent when the run started and present now is the run's, at
        whatever head it then holds.
        """
        if run_id not in branch:
            raise ValueError(f"{branch!r} does not carry the run id {run_id!r}")
        self._unique.append(branch)

    def receipts(self) -> frozenset[RemoteBranchReceipt]:
        """Every receipt, with the run-unique branches resolved now."""
        receipts = dict(self._receipts)
        pending = [branch for branch in self._unique if branch not in self._before]
        if pending:
            heads = self._read_heads()
            for branch in pending:
                if branch in heads:
                    receipts[branch] = RemoteBranchReceipt(branch, heads[branch])
        return frozenset(receipts.values())


def remote_heads(repo: str) -> dict[str, str]:
    """Every branch head of ``repo`` from ``git ls-remote``: one complete read
    that spends no GitHub API budget; raises rather than answer partially."""
    result = subprocess.run(
        ["git", "ls-remote", "--heads", github_remote(repo)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not list remote branches of {repo}: {result.stderr.strip()}")
    return parse_heads(result.stdout)


def parse_heads(ls_remote: str) -> dict[str, str]:
    """``{branch: sha}`` from ``git ls-remote --heads`` output."""
    heads: dict[str, str] = {}
    for line in ls_remote.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            heads[ref.removeprefix("refs/heads/")] = sha.strip()
    return heads
