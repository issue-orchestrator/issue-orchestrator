"""Unit tests for IssueLabelFilter."""

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.domain.issue_filter import IssueLabelFilter


def _make_issue(number: int, labels: list[str]) -> MagicMock:
    """Create a mock Issue with the given labels."""
    issue = MagicMock()
    issue.number = number
    issue.labels = labels
    return issue


class TestIssueLabelFilterInit:
    """Tests for IssueLabelFilter initialization."""

    def test_default_empty(self):
        """Default filter has no exclusions."""
        f = IssueLabelFilter()
        assert f.exclude_labels == frozenset()
        assert f.exclude_label_prefixes == ()
        assert f.is_empty()

    def test_from_config_with_list(self):
        """Create filter from list of exclude labels."""
        f = IssueLabelFilter.from_config(exclude_labels=["test-data", "wip"])
        assert f.exclude_labels == frozenset(["test-data", "wip"])
        assert f.exclude_label_prefixes == ()
        assert not f.is_empty()

    def test_from_config_with_prefixes(self):
        """Create filter from label-prefix exclusions."""
        f = IssueLabelFilter.from_config(exclude_label_prefixes=["io:e2e:", "tmp:"])
        assert f.exclude_labels == frozenset()
        assert f.exclude_label_prefixes == ("io:e2e:", "tmp:")
        assert not f.is_empty()

    def test_from_config_with_none(self):
        """Create filter with None exclude_labels."""
        f = IssueLabelFilter.from_config(exclude_labels=None)
        assert f.exclude_labels == frozenset()
        assert f.is_empty()

    def test_from_config_with_empty_list(self):
        """Create filter with empty exclude_labels list."""
        f = IssueLabelFilter.from_config(exclude_labels=[])
        assert f.exclude_labels == frozenset()
        assert f.is_empty()


class TestIssueLabelFilterApply:
    """Tests for IssueLabelFilter.apply()."""

    def test_empty_filter_passes_all(self):
        """Empty filter passes all issues through."""
        f = IssueLabelFilter()
        issues = [
            _make_issue(1, ["agent:web"]),
            _make_issue(2, ["agent:web", "test-data"]),
            _make_issue(3, ["bug"]),
        ]

        result = f.apply(issues)

        assert len(result) == 3
        assert [i.number for i in result] == [1, 2, 3]

    def test_excludes_single_label(self):
        """Filter excludes issues with a matching label."""
        f = IssueLabelFilter.from_config(exclude_labels=["test-data"])
        issues = [
            _make_issue(1, ["agent:web"]),
            _make_issue(2, ["agent:web", "test-data"]),
            _make_issue(3, ["bug"]),
        ]

        result = f.apply(issues)

        assert len(result) == 2
        assert [i.number for i in result] == [1, 3]

    def test_excludes_matching_label_prefix(self):
        """Filter excludes issues with labels matching an excluded prefix."""
        f = IssueLabelFilter.from_config(exclude_label_prefixes=["io:e2e:"])
        issues = [
            _make_issue(1, ["agent:web"]),
            _make_issue(2, ["agent:web", "io:e2e:isolated-4057"]),
            _make_issue(3, ["bug", "io:e2e:reviewed-4057"]),
        ]

        result = f.apply(issues)

        assert len(result) == 1
        assert [i.number for i in result] == [1]

    def test_exact_and_prefix_exclusions_work_together(self):
        """Exact-label and prefix exclusions both remove matching issues."""
        f = IssueLabelFilter.from_config(
            exclude_labels=["test-data"],
            exclude_label_prefixes=["io:e2e:"],
        )
        issues = [
            _make_issue(1, ["agent:web"]),
            _make_issue(2, ["agent:web", "test-data"]),
            _make_issue(3, ["agent:web", "io:e2e:isolated-4057"]),
        ]

        result = f.apply(issues)

        assert [i.number for i in result] == [1]

    def test_excludes_multiple_labels(self):
        """Filter excludes issues with any matching label."""
        f = IssueLabelFilter.from_config(exclude_labels=["test-data", "wip"])
        issues = [
            _make_issue(1, ["agent:web"]),
            _make_issue(2, ["agent:web", "test-data"]),
            _make_issue(3, ["bug", "wip"]),
            _make_issue(4, ["agent:web", "enhancement"]),
        ]

        result = f.apply(issues)

        assert len(result) == 2
        assert [i.number for i in result] == [1, 4]

    def test_excludes_with_multiple_matching_labels(self):
        """Issue with multiple excluded labels is still only removed once."""
        f = IssueLabelFilter.from_config(exclude_labels=["test-data", "wip"])
        issues = [
            _make_issue(1, ["agent:web"]),
            _make_issue(2, ["agent:web", "test-data", "wip"]),  # Has both excluded labels
        ]

        result = f.apply(issues)

        assert len(result) == 1
        assert result[0].number == 1

    def test_case_sensitive(self):
        """Label matching is case-sensitive."""
        f = IssueLabelFilter.from_config(exclude_labels=["Test-Data"])
        issues = [
            _make_issue(1, ["test-data"]),  # Different case
            _make_issue(2, ["Test-Data"]),  # Exact match
        ]

        result = f.apply(issues)

        assert len(result) == 1
        assert result[0].number == 1

    def test_empty_issues_list(self):
        """Filter handles empty issues list."""
        f = IssueLabelFilter.from_config(exclude_labels=["test-data"])

        result = f.apply([])

        assert result == []

    def test_no_matches(self):
        """Filter returns all issues if no exclusion matches."""
        f = IssueLabelFilter.from_config(exclude_labels=["nonexistent-label"])
        issues = [
            _make_issue(1, ["agent:web"]),
            _make_issue(2, ["bug"]),
        ]

        result = f.apply(issues)

        assert len(result) == 2

    def test_exclusion_reason_for_exact_label(self):
        """Filter exposes the exact-label exclusion detail for callers."""
        f = IssueLabelFilter.from_config(exclude_labels=["test-data"])
        issue = _make_issue(1, ["agent:web", "test-data"])

        assert f.exclusion_reason(issue) == 'has excluded label "test-data"'

    def test_exclusion_reason_for_prefix(self):
        """Filter exposes the prefix exclusion detail for callers."""
        f = IssueLabelFilter.from_config(exclude_label_prefixes=["io:e2e:"])
        issue = _make_issue(1, ["agent:web", "io:e2e:isolated-4057"])

        assert (
            f.exclusion_reason(issue)
            == 'has label "io:e2e:isolated-4057" matching excluded prefix "io:e2e:"'
        )

    def test_exclusion_reason_none_when_issue_passes(self):
        """Filter returns no exclusion detail when an issue is retained."""
        f = IssueLabelFilter.from_config(exclude_labels=["test-data"])
        issue = _make_issue(1, ["agent:web"])

        assert f.exclusion_reason(issue) is None


class TestIssueLabelFilterRepr:
    """Tests for IssueLabelFilter.__repr__()."""

    def test_empty_repr(self):
        """Empty filter has simple repr."""
        f = IssueLabelFilter()
        assert repr(f) == "IssueLabelFilter()"

    def test_with_labels_repr(self):
        """Filter with labels shows sorted list."""
        f = IssueLabelFilter.from_config(exclude_labels=["wip", "test-data"])
        assert repr(f) == "IssueLabelFilter(exclude=['test-data', 'wip'])"

    def test_with_prefixes_repr(self):
        """Filter repr includes label-prefix exclusions."""
        f = IssueLabelFilter.from_config(exclude_label_prefixes=["io:e2e:"])
        assert repr(f) == "IssueLabelFilter(exclude_prefixes=['io:e2e:'])"
