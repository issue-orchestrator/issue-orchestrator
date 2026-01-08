"""Unit tests for centralized terminal naming module."""

import pytest

from issue_orchestrator.adapters.terminal.naming import (
    SessionType,
    ParsedSessionName,
    terminal_id,
    parse_terminal_id,
    iterm_tab_name,
    iterm_tab_prefix,
    number_from_terminal_id,
)


class TestTerminalId:
    """Tests for terminal_id generation."""

    def test_terminal_id_with_string(self):
        """Generate terminal_id from string type."""
        assert terminal_id("issue", 123) == "issue-123"
        assert terminal_id("review", 456) == "review-456"
        assert terminal_id("rework", 789) == "rework-789"

    def test_terminal_id_with_enum(self):
        """Generate terminal_id from SessionType enum."""
        assert terminal_id(SessionType.ISSUE, 123) == "issue-123"
        assert terminal_id(SessionType.REVIEW, 456) == "review-456"
        assert terminal_id(SessionType.TRIAGE, 101) == "triage-101"


class TestParseTerminalId:
    """Tests for parse_terminal_id parsing."""

    def test_parse_issue_session(self):
        """Parse issue session terminal_id."""
        result = parse_terminal_id("issue-123")
        assert result is not None
        assert result.session_type == SessionType.ISSUE
        assert result.number == 123

    def test_parse_review_session(self):
        """Parse review session terminal_id."""
        result = parse_terminal_id("review-456")
        assert result is not None
        assert result.session_type == SessionType.REVIEW
        assert result.number == 456

    def test_parse_rework_session(self):
        """Parse rework session terminal_id."""
        result = parse_terminal_id("rework-789")
        assert result is not None
        assert result.session_type == SessionType.REWORK
        assert result.number == 789

    def test_parse_invalid_no_hyphen(self):
        """Return None for invalid format without hyphen."""
        assert parse_terminal_id("issue123") is None

    def test_parse_invalid_non_numeric(self):
        """Return None for non-numeric suffix."""
        assert parse_terminal_id("issue-abc") is None

    def test_parse_invalid_unknown_type(self):
        """Return None for unknown session type."""
        assert parse_terminal_id("unknown-123") is None


class TestItermTabName:
    """Tests for iterm_tab_name generation."""

    def test_tab_name_without_title(self):
        """Generate tab name without title."""
        assert iterm_tab_name(123) == "#123"

    def test_tab_name_with_short_title(self):
        """Generate tab name with short title."""
        assert iterm_tab_name(123, "Fix bug") == "#123 Fix bug"

    def test_tab_name_with_long_title(self):
        """Generate tab name with long title (truncated)."""
        long_title = "This is a very long title that should be truncated"
        result = iterm_tab_name(123, long_title)
        assert result.startswith("#123 ")
        assert len(result) <= 25  # "#123 " + 20 chars max

    def test_tab_name_custom_max_length(self):
        """Generate tab name with custom max title length."""
        result = iterm_tab_name(123, "Hello World", max_title_length=5)
        assert result == "#123 Hello"


class TestItermTabPrefix:
    """Tests for iterm_tab_prefix generation."""

    def test_tab_prefix(self):
        """Generate correct tab prefix."""
        assert iterm_tab_prefix(123) == "#123"
        assert iterm_tab_prefix(456) == "#456"
        assert iterm_tab_prefix(1) == "#1"


class TestNumberFromTerminalId:
    """Tests for number_from_terminal_id extraction."""

    def test_extract_from_issue(self):
        """Extract number from issue terminal_id."""
        assert number_from_terminal_id("issue-123") == 123

    def test_extract_from_review(self):
        """Extract number from review terminal_id."""
        assert number_from_terminal_id("review-456") == 456

    def test_extract_invalid_returns_none(self):
        """Return None for invalid format."""
        assert number_from_terminal_id("invalid") is None
        assert number_from_terminal_id("issue-abc") is None


class TestParsedSessionName:
    """Tests for ParsedSessionName dataclass."""

    def test_terminal_id_property(self):
        """Get terminal_id from parsed session name."""
        parsed = ParsedSessionName(SessionType.ISSUE, 123)
        assert parsed.terminal_id == "issue-123"

    def test_iterm_tab_prefix_property(self):
        """Get iTerm tab prefix from parsed session name."""
        parsed = ParsedSessionName(SessionType.REVIEW, 456)
        assert parsed.iterm_tab_prefix == "#456"

    def test_frozen_dataclass(self):
        """ParsedSessionName is immutable."""
        parsed = ParsedSessionName(SessionType.ISSUE, 123)
        with pytest.raises(Exception):  # FrozenInstanceError
            parsed.number = 456
