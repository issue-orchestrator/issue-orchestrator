"""Unit tests for GitHubAdapter caching behavior."""

from issue_orchestrator.config import Config
from issue_orchestrator.execution.github_adapter import GitHubAdapter
from issue_orchestrator.ports.pull_request_tracker import PRInfo


def test_update_label_cache_updates_pr_cache() -> None:
    config = Config()
    config.github_token = "token"
    config.queue_refresh_seconds = 60
    adapter = GitHubAdapter("owner/repo", config=config)

    pr_info = PRInfo(
        number=10,
        title="Test PR",
        url="https://github.com/owner/repo/pull/10",
        branch="1-test",
        body="body",
        state="open",
        labels=["old-label"],
    )
    adapter._issue_pr_cache[1] = pr_info
    adapter._branch_pr_cache["1-test"] = pr_info

    adapter.update_label_cache(1, ["new-label"])

    assert pr_info.labels == ["new-label"]
    assert adapter._branch_pr_cache["1-test"].labels == ["new-label"]
