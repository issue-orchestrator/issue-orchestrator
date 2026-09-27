"""A registry row whose run predates run-role recording never writes to GitHub.

Such a run's ledger row is old-shaped: the role columns came later (#7189), so
the additive migration leaves them NULL. Agents are PTY children and do not
survive an engine stop, so a registry entry like this at startup is stale,
not a live session. It used to fail restoration and then be quarantined as an
unrestorable live run - a GitHub block and comment on the first tick. It is
now treated as ended: no restore, no quarantine, its claim requeued by the
dead-run sweep.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from issue_orchestrator.control.session_restorer import SessionRestorer
from issue_orchestrator.control.session_routing import restore_running_sessions
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.ports.session_runner import DiscoveredSession
from tests.unit.test_provider_readiness_boundary import _quarantine, _routed_rework
from tests.unit.test_session_restorer import MockRepositoryHost, MockWorkingCopy

_ROLE_COLUMNS = ("agent_label", "completion_task", "rework_pr_number", "rework_cycle")


def _pre_role_ledger(tmp_path: Path, harness, session) -> SqliteIssueRunLedger:
    """The run's ledger as pre-role code left it, opened by today's code: the
    table is cut back to its pre-role shape and reopened, so the real additive
    migration re-adds the role columns NULL."""
    path = tmp_path / "pre-role" / "runs.sqlite"
    record = harness.run_ledger.recorded_run(session.run_assets)
    SqliteIssueRunLedger(path, repo_slug="test/repo").record_run(session.issue.number, record)
    with sqlite3.connect(path) as conn:
        for column in _ROLE_COLUMNS:
            conn.execute(f"ALTER TABLE issue_runs DROP COLUMN {column}")
    reopened = SqliteIssueRunLedger(path, repo_slug="test/repo")
    assert reopened.recorded_run(session.run_assets).agent_label is None
    return reopened


def test_a_pre_role_registry_row_is_treated_as_ended_without_a_github_write(
    tmp_path: Path,
) -> None:
    harness, session = _routed_rework(tmp_path)  # holds a readable queued-work claim
    ledger = _pre_role_ledger(tmp_path, harness, session)
    repo_host = MockRepositoryHost()
    repo_host.issues[session.issue.number] = session.issue
    working_copy = MockWorkingCopy()
    working_copy.branches[session.worktree_path] = session.branch_name
    restorer = SessionRestorer(harness.launcher.config, repo_host, working_copy, run_ledger=ledger)
    restarted = OrchestratorState()
    registry_entry = DiscoveredSession(
        issue_number=session.issue.number, tab_name="", is_review=False,
        session_name=session.terminal_id, run_dir=str(session.run_assets.run_dir),
    )

    restore_running_sessions(
        [registry_entry], restarted, restorer, harness.claims, _quarantine(harness)
    )

    assert harness.quarantine_actions == []  # no block, no needs-human, no comment
    assert restarted.active_sessions == []
    # Its queued work goes back on the queue through the dead-run sweep.
    assert [item.pr_number for item in restarted.pending_reworks] == [70]
