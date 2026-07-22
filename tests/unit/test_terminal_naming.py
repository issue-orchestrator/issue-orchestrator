"""Unit tests for centralized terminal naming module."""

import pytest

from issue_orchestrator.adapters.terminal.naming import (
    SessionType,
    ParsedSessionName,
    terminal_id,
    parse_terminal_id,
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
        assert terminal_id(SessionType.TECH_LEAD, 101) == "tech-lead-101"


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

    def test_frozen_dataclass(self):
        """ParsedSessionName is immutable."""
        parsed = ParsedSessionName(SessionType.ISSUE, 123)
        with pytest.raises(Exception):  # FrozenInstanceError
            parsed.number = 456
