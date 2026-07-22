"""Composition-root wiring for the board snapshot's session-activity probe.

``_make_session_activity_reader`` closes over the working-copy port and reads
two best-effort hung-EVIDENCE signals per active session: the mtime of the
session's terminal recording (its agent output stream, a "last observable
activity" proxy) and the commit count on its branch ahead of base. These tests
pin both the happy read and every degradation: a real ``0`` stays distinct from
"could not read", a missing recording yields no activity timestamp, an absent
worktree or a raising working copy yields the unknown commits sentinel — and
none of these raise (the snapshot must never break on missing evidence).
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

from issue_orchestrator.domain.board_snapshot import COMMITS_AHEAD_UNKNOWN
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    Session,
    SessionKey,
    TaskKind,
)
from issue_orchestrator.entrypoints.bootstrap_tech_lead import (
    _make_session_activity_reader,
)
from issue_orchestrator.ports.git import GitError, GitResult
from issue_orchestrator.ports.working_copy import CommitInfo
from tests.unit.session_run_helpers import make_session_run_assets


class _FakeWorkingCopy:
    """Duck-typed ``WorkingCopy`` exposing only the probe's one method."""

    def __init__(
        self,
        *,
        commits: list[CommitInfo] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._commits = commits or []
        self._error = error
        self.calls: list[Path] = []

    def get_commits_ahead_of_main(self, worktree: Path) -> list[CommitInfo]:
        self.calls.append(worktree)
        if self._error is not None:
            raise self._error
        return self._commits


def _commit(sha: str = "abc123") -> CommitInfo:
    return CommitInfo(sha=sha, message="msg", author="me", short_sha=sha[:7])


def _make_session(base: Path) -> Session:
    run_assets = make_session_run_assets(base)
    return Session(
        key=SessionKey(issue=FakeIssueKey("101"), task=TaskKind.CODE),
        issue=Issue(number=101, title="Test issue", labels=["agent:test"]),
        agent_config=AgentConfig(prompt_path=base / "prompt.md", model="sonnet"),
        terminal_id="issue-101",
        worktree_path=run_assets.worktree_path,
        branch_name="101-test",
        run_assets=run_assets,
    )


def test_reads_recording_mtime_and_commit_count(tmp_path: Path) -> None:
    session = _make_session(tmp_path / "wt")
    stamp = datetime(2026, 7, 10, 11, 30, 0).timestamp()
    os.utime(session.run_assets.terminal_recording.path, (stamp, stamp))
    working_copy = _FakeWorkingCopy(commits=[_commit("a"), _commit("b")])

    facts = _make_session_activity_reader(working_copy)(session)

    assert facts is not None
    assert facts.commits_ahead == 2
    assert facts.last_activity_at == datetime.fromtimestamp(stamp).isoformat()
    assert working_copy.calls == [session.worktree_path]


def test_zero_commits_is_a_real_zero_not_unknown(tmp_path: Path) -> None:
    """An empty commit list on an existing worktree is a genuine 0 (hang signal
    when paired with idle), never the unknown sentinel."""
    session = _make_session(tmp_path / "wt")

    facts = _make_session_activity_reader(_FakeWorkingCopy(commits=[]))(session)

    assert facts is not None
    assert facts.commits_ahead == 0


def test_missing_recording_yields_no_activity_timestamp(tmp_path: Path) -> None:
    session = _make_session(tmp_path / "wt")
    session.run_assets.terminal_recording.path.unlink()

    facts = _make_session_activity_reader(_FakeWorkingCopy(commits=[_commit()]))(
        session
    )

    assert facts is not None
    assert facts.last_activity_at is None
    assert facts.commits_ahead == 1  # commits still readable


def test_absent_worktree_yields_unknown_commits(tmp_path: Path) -> None:
    """A gone worktree short-circuits to the unknown sentinel BEFORE calling the
    working copy — a missing read must never masquerade as a real 0."""
    session = _make_session(tmp_path / "wt")
    shutil.rmtree(session.worktree_path)
    working_copy = _FakeWorkingCopy(commits=[_commit()])

    facts = _make_session_activity_reader(working_copy)(session)

    assert facts is not None
    assert facts.commits_ahead == COMMITS_AHEAD_UNKNOWN
    assert facts.last_activity_at is None  # recording went with the worktree
    assert working_copy.calls == []  # never consulted for a missing worktree


def test_working_copy_git_error_yields_unknown_commits(tmp_path: Path) -> None:
    session = _make_session(tmp_path / "wt")
    git_error = GitError(
        GitResult(argv=["git", "log"], returncode=128, stdout="", stderr="boom")
    )
    reader = _make_session_activity_reader(_FakeWorkingCopy(error=git_error))

    facts = reader(session)

    assert facts is not None
    assert facts.commits_ahead == COMMITS_AHEAD_UNKNOWN
