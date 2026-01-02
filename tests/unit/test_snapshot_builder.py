"""Unit tests for SnapshotBuilder."""

from unittest.mock import MagicMock, Mock, call

from issue_orchestrator.config import Config
from issue_orchestrator.control.snapshot_builder import SnapshotBuilder
from issue_orchestrator.models import Issue, OrchestratorState


def test_snapshot_builder_multiple_milestones_dedupes():
    """Fetch across milestones without duplicating issues."""
    config = Config()
    config.agents = {"agent:web": Mock()}
    config.filter_milestones = ["M1", "M2"]
    config.issue_fetch_limit = 25

    repository_host = MagicMock()
    issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
    issue_2 = Issue(number=2, title="Issue 2", labels=["agent:web"])
    repository_host.list_issues.side_effect = [
        [issue_1],
        [issue_1, issue_2],
    ]
    repository_host.get_prs_for_issue.return_value = []

    builder = SnapshotBuilder(config=config, repository_host=repository_host)
    snapshot = builder.build_snapshot(
        state=OrchestratorState(),
        snapshot_id=1,
        last_tick_id=2,
    )

    assert set(snapshot["issues"].keys()) == {"1", "2"}
    assert repository_host.list_issues.call_args_list == [
        call(labels=["agent:web"], milestone="M1", limit=25),
        call(labels=["agent:web"], milestone="M2", limit=25),
    ]
