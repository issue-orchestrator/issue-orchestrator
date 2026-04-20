from types import SimpleNamespace
from unittest.mock import MagicMock

from issue_orchestrator.control.github_workflow import GitHubWorkflow
from issue_orchestrator.events import EventContext
from issue_orchestrator.infra.config import Config


def test_scan_pending_pr_work_loads_issue_branches_once_and_reuses_map() -> None:
    pr_scanner = MagicMock()
    issue_branches = {42: "42-scratch-1774101016"}
    pr_scanner.load_issue_branches.return_value = issue_branches
    pr_scanner.scan_for_reviews.return_value = []
    pr_scanner.scan_for_reworks.return_value = ([], [])

    workflow = GitHubWorkflow(
        config=Config(),
        events=MagicMock(),
        repository_host=MagicMock(),
        fact_gatherer=MagicMock(),
        pr_scanner=pr_scanner,
        label_sync=None,
        event_context=EventContext(),
    )
    state = SimpleNamespace(
        pending_reviews=[],
        pending_reworks=[],
        active_sessions=[],
        discovered_reviews=[],
        discovered_reworks=[],
        discovered_escalations=[],
        session_history=[],
        issue_refresh_timestamps={},
        issue_last_refreshed_at={},
    )

    workflow.scan_pending_pr_work(state)

    pr_scanner.load_issue_branches.assert_called_once_with()
    pr_scanner.scan_for_reviews.assert_called_once_with(
        [],
        [],
        issue_branches=issue_branches,
    )
    pr_scanner.scan_for_reworks.assert_called_once_with(
        [],
        [],
        issue_branches=issue_branches,
    )
