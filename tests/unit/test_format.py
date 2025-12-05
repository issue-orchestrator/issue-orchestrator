"""Tests for formatting utilities."""

import pytest

from issue_orchestrator.format import format_duration, format_issue_number, truncate_string


class TestFormatDuration:
    """Tests for format_duration function."""

    def test_seconds_only(self):
        """Test formatting durations under 60 seconds."""
        assert format_duration(0) == "0s"
        assert format_duration(1) == "1s"
        assert format_duration(30) == "30s"
        assert format_duration(59) == "59s"

    def test_minutes_only(self):
        """Test formatting durations in minutes."""
        assert format_duration(60) == "1m"
        assert format_duration(120) == "2m"
        assert format_duration(300) == "5m"
        assert format_duration(3540) == "59m"

    def test_minutes_and_seconds(self):
        """Test formatting durations with minutes and seconds."""
        assert format_duration(61) == "1m 1s"
        assert format_duration(125) == "2m 5s"
        assert format_duration(3599) == "59m 59s"

    def test_hours_only(self):
        """Test formatting durations in hours."""
        assert format_duration(3600) == "1h"
        assert format_duration(7200) == "2h"
        assert format_duration(36000) == "10h"

    def test_hours_and_minutes(self):
        """Test formatting durations with hours and minutes."""
        assert format_duration(3660) == "1h 1m"
        assert format_duration(5400) == "1h 30m"
        assert format_duration(36900) == "10h 15m"


class TestFormatIssueNumber:
    """Tests for format_issue_number function."""

    def test_format_issue_number(self):
        """Test formatting issue numbers."""
        assert format_issue_number(1) == "#1"
        assert format_issue_number(232) == "#232"
        assert format_issue_number(9999) == "#9999"


class TestTruncateString:
    """Tests for truncate_string function."""

    def test_string_shorter_than_max(self):
        """Test strings that don't need truncation."""
        assert truncate_string("hello", 10) == "hello"
        assert truncate_string("short", 20) == "short"

    def test_string_at_max_length(self):
        """Test strings at exactly max length."""
        assert truncate_string("hello", 5) == "hello"

    def test_string_longer_than_max(self):
        """Test strings that need truncation."""
        assert truncate_string("hello world", 5) == "he..."
        assert truncate_string("hello world", 8) == "hello..."

    def test_custom_suffix(self):
        """Test with custom suffix."""
        assert truncate_string("hello world", 8, suffix="→") == "hello w→"
        assert truncate_string("hello world", 5, suffix=">>") == "hel>>"

    def test_default_max_length(self):
        """Test with default max_length of 80."""
        short_text = "a" * 50
        assert truncate_string(short_text) == short_text

        long_text = "a" * 100
        truncated = truncate_string(long_text)
        assert len(truncated) == 80
        assert truncated.endswith("...")
