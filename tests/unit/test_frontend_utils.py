"""Unit tests for frontend utility functions."""

import pytest
from datetime import datetime
from issue_orchestrator.frontend_utils import (
    format_date,
    truncate_text,
    parse_status_badge_class,
    format_issue_for_display,
)


class TestFormatDate:
    """Test date formatting utility."""

    def test_format_date_with_datetime_object(self):
        """Test formatting a datetime object."""
        dt = datetime(2025, 12, 5, 14, 30, 0)
        result = format_date(dt)
        assert "Dec" in result
        assert "05" in result
        assert "2025" in result

    def test_format_date_with_iso_string(self):
        """Test formatting an ISO format string."""
        iso_string = "2025-12-05T14:30:00"
        result = format_date(iso_string)
        assert "Dec" in result
        assert "05" in result
        assert "2025" in result


class TestTruncateText:
    """Test text truncation utility."""

    def test_truncate_short_text(self):
        """Test that short text is not truncated."""
        text = "Short text"
        result = truncate_text(text, max_length=50)
        assert result == text
        assert "..." not in result

    def test_truncate_long_text(self):
        """Test that long text is truncated."""
        text = "This is a very long text that exceeds the maximum length"
        result = truncate_text(text, max_length=20)
        assert len(result) <= 23  # 20 + "..."
        assert "..." in result

    def test_truncate_custom_length(self):
        """Test truncation with custom max length."""
        text = "Hello World"
        result = truncate_text(text, max_length=5)
        assert result == "Hello..."


class TestParseStatusBadgeClass:
    """Test status badge CSS class mapping."""

    def test_running_status(self):
        """Test running status mapping."""
        assert parse_status_badge_class("running") == "status-running"

    def test_completed_status(self):
        """Test completed status mapping."""
        assert parse_status_badge_class("completed") == "status-completed"

    def test_failed_status(self):
        """Test failed status mapping."""
        assert parse_status_badge_class("failed") == "status-failed"

    def test_case_insensitive(self):
        """Test that status matching is case insensitive."""
        assert parse_status_badge_class("RUNNING") == "status-running"
        assert parse_status_badge_class("Failed") == "status-failed"

    def test_unknown_status(self):
        """Test unknown status defaults to default class."""
        assert parse_status_badge_class("unknown") == "status-default"


class TestFormatIssueForDisplay:
    """Test issue data formatting for display."""

    def test_format_issue_basic(self):
        """Test basic issue formatting."""
        issue_data = {
            'number': 123,
            'title': 'Test Issue Title',
            'state': 'open',
            'created_at': '2025-12-05T10:00:00',
            'updated_at': '2025-12-05T14:00:00',
            'labels': ['bug', 'priority:high'],
        }
        result = format_issue_for_display(issue_data)

        assert result['number'] == 123
        assert result['full_title'] == 'Test Issue Title'
        assert result['state'] == 'open'
        assert result['labels'] == ['bug', 'priority:high']

    def test_format_issue_truncates_long_title(self):
        """Test that issue title is truncated for display."""
        issue_data = {
            'number': 456,
            'title': 'This is an extremely long issue title that should be truncated for display purposes in the dashboard',
            'state': 'open',
            'created_at': '2025-12-05T10:00:00',
            'updated_at': '2025-12-05T14:00:00',
        }
        result = format_issue_for_display(issue_data)

        assert result['title'] != result['full_title']
        assert '...' in result['title']

    def test_format_issue_sets_status_class(self):
        """Test that status class is correctly set."""
        issue_data = {
            'number': 789,
            'title': 'Issue',
            'state': 'open',
            'created_at': '2025-12-05T10:00:00',
            'updated_at': '2025-12-05T14:00:00',
        }
        result = format_issue_for_display(issue_data)
        # 'open' is not in the status_map, so it should default
        assert 'status' in result['status_class']

    def test_format_issue_missing_fields(self):
        """Test formatting with missing optional fields."""
        issue_data = {'number': 999}
        result = format_issue_for_display(issue_data)

        assert result['number'] == 999
        assert result['title'] == ''
        assert result['labels'] == []
