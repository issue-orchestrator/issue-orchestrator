"""Unit tests for configured issue-scope decisions."""

from pathlib import Path

from issue_orchestrator.control.issue_scope import (
    evaluate_issue_scope,
    issue_scope_skip_detail,
)
from issue_orchestrator.domain.models import AgentConfig, Issue
from issue_orchestrator.infra.config import Config


def _make_config() -> Config:
    return Config(
        repo="owner/repo",
        repo_root=Path("/tmp/repo"),
        worktree_base=Path("/tmp/worktrees"),
        agents={"agent:web": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))},
    )


def test_scope_rejects_closed_issue_by_default() -> None:
    config = _make_config()
    issue = Issue(number=1, title="Closed", labels=["agent:web"], state="closed")

    assert issue_scope_skip_detail(config, issue) == "issue is closed"


def test_scope_can_evaluate_closed_issue_as_reopen_candidate() -> None:
    config = _make_config()
    issue = Issue(number=1, title="Closed", labels=["agent:web"], state="closed")

    assert issue_scope_skip_detail(config, issue, require_open=False) is None


def test_scope_reports_missing_required_filter_label() -> None:
    config = _make_config()
    config.filtering.label = "redo-poorly-reviewed"
    issue = Issue(number=1, title="No mark", labels=["agent:web"])

    decision = evaluate_issue_scope(config, issue)

    assert decision.in_scope is False
    assert decision.code == "missing_filter_label"
    assert (
        issue_scope_skip_detail(config, issue)
        == 'missing required filter label "redo-poorly-reviewed"'
    )


def test_scope_reports_milestone_mismatch() -> None:
    config = _make_config()
    config.filtering.milestones = ["M1", "M2"]
    issue = Issue(
        number=1, title="Wrong milestone", labels=["agent:web"], milestone="M3"
    )

    assert (
        issue_scope_skip_detail(config, issue) == 'milestone "M3" is not one of M1, M2'
    )


def test_scope_can_omit_milestone_filter_for_review_pr_scope() -> None:
    config = _make_config()
    config.filtering.milestones = ["M1", "M2"]
    issue = Issue(
        number=1, title="Wrong milestone", labels=["agent:web"], milestone="M3"
    )

    assert (
        issue_scope_skip_detail(config, issue, include_milestone_filter=False) is None
    )


def test_scope_uses_configured_label_filter_reason() -> None:
    config = _make_config()
    config.filtering.exclude_label_prefixes = ["io:e2e:"]
    issue = Issue(
        number=1,
        title="Test data",
        labels=["agent:web", "io:e2e:isolated-4057"],
    )

    assert (
        issue_scope_skip_detail(config, issue)
        == 'has label "io:e2e:isolated-4057" matching excluded prefix "io:e2e:"'
    )


def test_scope_can_include_single_issue_filter() -> None:
    config = _make_config()
    config.filtering.issue = 2
    issue = Issue(number=1, title="Other issue", labels=["agent:web"])

    assert (
        issue_scope_skip_detail(config, issue, include_issue_number_filter=True)
        == "engine is scoped to issue #2"
    )


def test_scope_omits_single_issue_filter_for_queue_snapshots() -> None:
    config = _make_config()
    config.filtering.issue = 2
    issue = Issue(number=1, title="Other issue", labels=["agent:web"])

    assert issue_scope_skip_detail(config, issue) is None
