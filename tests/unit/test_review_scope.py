"""Unit tests for review PR scope checks."""

from issue_orchestrator.control.review_scope import (
    ReviewScopeChecker,
    extract_issue_number_from_pr,
    pr_fields_reference_issue,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo


class IssueReader:
    def __init__(self, *issues: Issue):
        self.issues = {issue.number: issue for issue in issues}
        self.calls: list[int] = []

    def get_issue(self, issue_number: int) -> Issue | None:
        self.calls.append(issue_number)
        return self.issues.get(issue_number)


def _issue(number: int, labels: list[str], state: str = "open") -> Issue:
    return Issue(number=number, title=f"Issue {number}", labels=labels, state=state)


def _pr(number: int, branch: str = "", body: str = "") -> PRInfo:
    return PRInfo(
        number=number,
        title=f"PR {number}",
        url=f"https://example.test/pull/{number}",
        branch=branch,
        body=body,
        state="open",
        labels=[],
    )


def test_extract_issue_number_prefers_branch_over_body() -> None:
    pr = _pr(99, branch="123-feature", body="Closes #456")

    assert extract_issue_number_from_pr(pr) == 123


def test_review_scope_with_no_filters_does_not_fetch_issue() -> None:
    config = Config()
    reader = IssueReader()

    result = ReviewScopeChecker(config, reader, log_prefix="test").check_issue_number(123, 456)

    assert result.in_scope is True
    assert reader.calls == []


def test_review_scope_with_open_issue_required_fetches_without_filters() -> None:
    config = Config()
    reader = IssueReader(_issue(123, ["agent:web"], state="closed"))

    result = ReviewScopeChecker(
        config,
        reader,
        log_prefix="test",
        require_open_issue=True,
    ).check_issue_number(123, 456)

    assert result.in_scope is False
    assert result.reason == "issue_not_open"
    assert reader.calls == [123]


def test_review_scope_skips_issue_missing_filter_label() -> None:
    config = Config()
    config.filtering.label = "io:e2e:isolated"
    reader = IssueReader(_issue(123, ["io-e2e-test-data"]))

    result = ReviewScopeChecker(config, reader, log_prefix="test").check_issue_number(123, 456)

    assert result.in_scope is False
    assert result.reason == "missing_filter_label"
    assert result.issue_number == 123


def test_review_scope_allows_closed_issue_by_default_when_issue_must_be_fetched() -> None:
    config = Config()
    config.filtering.label = "io-e2e-test-data"
    reader = IssueReader(_issue(123, ["io-e2e-test-data"], state="closed"))

    result = ReviewScopeChecker(config, reader, log_prefix="test").check_issue_number(123, 456)

    assert result.in_scope is True
    assert result.reason == "ok"


def test_review_scope_skips_closed_issue_when_open_issue_required() -> None:
    config = Config()
    config.filtering.label = "io-e2e-test-data"
    reader = IssueReader(_issue(123, ["io-e2e-test-data"], state="closed"))

    result = ReviewScopeChecker(
        config,
        reader,
        log_prefix="test",
        require_open_issue=True,
    ).check_issue_number(123, 456)

    assert result.in_scope is False
    assert result.reason == "issue_not_open"


def test_review_scope_applies_exclude_prefixes() -> None:
    config = Config()
    config.filtering.exclude_label_prefixes = ["io:e2e:"]
    reader = IssueReader(_issue(123, ["agent:web", "io:e2e:isolated"]))

    result = ReviewScopeChecker(config, reader, log_prefix="test").check_issue_number(123, 456)

    assert result.in_scope is False
    assert result.reason == "excluded_by_label_filter"


def test_pr_fields_reference_issue_uses_exact_issue_boundaries() -> None:
    assert pr_fields_reference_issue(
        branch="123-feature",
        title="#999: Other PR",
        body="Closes #999",
        issue_numbers=[123],
    )
    assert pr_fields_reference_issue(
        branch="feature",
        title="#999: Other PR",
        body="closes   #123",
        issue_numbers=[123],
    )
    assert not pr_fields_reference_issue(
        branch="feature",
        title="#1234: Other PR",
        body="Closes #1234",
        issue_numbers=[123],
    )
