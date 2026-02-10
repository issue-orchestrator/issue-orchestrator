"""Tests for audit module functionality."""

from unittest.mock import MagicMock

from issue_orchestrator.infra.audit import (
    get_issue_dependencies,
    IssueDependencyInfo,
)
from issue_orchestrator.domain.models import Issue


def make_issue(
    number: int,
    title: str = "",
    body: str = "",
) -> Issue:
    """Create a mock Issue for testing."""
    return Issue(
        number=number,
        title=title or f"Issue #{number}",
        body=body,
        state="open",
        labels=["agent:backend"],
    )


class TestGetIssueDependencies:
    """Tests for get_issue_dependencies function."""

    def test_no_dependencies(self):
        """Issue without dependencies returns empty info."""
        config = MagicMock()
        issues = [make_issue(1, body="No dependencies here")]

        result = get_issue_dependencies(issues, config)  # type: ignore

        assert 1 in result
        assert result[1].has_dependencies is False
        assert result[1].dependencies == []
        assert result[1].summary == ""

    def test_single_dependency(self):
        """Issue with single dependency is detected."""
        config = MagicMock()
        issues = [
            make_issue(1, title="First issue"),
            make_issue(2, body="Depends-on: #1"),
        ]

        result = get_issue_dependencies(issues, config)  # type: ignore

        assert result[2].has_dependencies is True
        assert len(result[2].dependencies) == 1
        assert result[2].dependencies[0][0] == 1
        assert result[2].dependencies[0][1] == "First issue"
        assert "#1" in result[2].summary

    def test_multiple_dependencies(self):
        """Issue with multiple dependencies detects all."""
        config = MagicMock()
        issues = [
            make_issue(1, title="First"),
            make_issue(2, title="Second"),
            make_issue(3, body="Depends-on: #1\nDepends-on: #2"),
        ]

        result = get_issue_dependencies(issues, config)  # type: ignore

        assert result[3].has_dependencies is True
        assert len(result[3].dependencies) == 2
        dep_nums = [d[0] for d in result[3].dependencies]
        assert 1 in dep_nums
        assert 2 in dep_nums
        assert "#1" in result[3].summary
        assert "#2" in result[3].summary

    def test_dependency_on_unknown_issue(self):
        """Dependency on unknown issue shows generic title."""
        config = MagicMock()
        issues = [
            make_issue(2, body="Depends-on: #999"),
        ]

        result = get_issue_dependencies(issues, config)  # type: ignore

        assert result[2].has_dependencies is True
        assert result[2].dependencies[0][0] == 999
        assert result[2].dependencies[0][1] == "Issue #999"

    def test_cross_repo_dependency(self):
        """Cross-repo dependency shows repo in title."""
        config = MagicMock()
        issues = [
            make_issue(1, body="Depends-on: owner/other-repo#42"),
        ]

        result = get_issue_dependencies(issues, config)  # type: ignore

        assert result[1].has_dependencies is True
        assert result[1].dependencies[0][0] == 42
        assert result[1].dependencies[0][1] == "owner/other-repo#42"

    def test_empty_body(self):
        """Issue with empty body returns no dependencies."""
        config = MagicMock()
        issues = [make_issue(1, body="")]

        result = get_issue_dependencies(issues, config)  # type: ignore

        assert result[1].has_dependencies is False

    def test_none_body(self):
        """Issue with None body returns no dependencies."""
        config = MagicMock()
        issue = make_issue(1)
        issue.body = None
        issues = [issue]

        result = get_issue_dependencies(issues, config)  # type: ignore

        assert result[1].has_dependencies is False

    def test_all_issues_processed(self):
        """All issues in list are processed."""
        config = MagicMock()
        issues = [
            make_issue(1, body=""),
            make_issue(2, body="Depends-on: #1"),
            make_issue(3, body="No deps"),
        ]

        result = get_issue_dependencies(issues, config)  # type: ignore

        assert len(result) == 3
        assert 1 in result
        assert 2 in result
        assert 3 in result


class TestIssueDependencyInfo:
    """Tests for IssueDependencyInfo dataclass."""

    def test_default_values(self):
        """Default values are set correctly."""
        info = IssueDependencyInfo(issue_number=1)

        assert info.issue_number == 1
        assert info.has_dependencies is False
        assert info.dependencies == []
        assert info.summary == ""

    def test_with_dependencies(self):
        """Info with dependencies stores correctly."""
        info = IssueDependencyInfo(
            issue_number=1,
            has_dependencies=True,
            dependencies=[(2, "Dep issue")],
            summary="Depends on: #2",
        )

        assert info.has_dependencies is True
        assert info.dependencies == [(2, "Dep issue")]
