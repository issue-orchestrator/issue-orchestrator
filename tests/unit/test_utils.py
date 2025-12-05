"""Tests for utility functions."""

import pytest

from issue_orchestrator.utils import format_issue_reference, parse_issue_reference


class TestFormatIssueReference:
    """Tests for format_issue_reference function."""

    def test_format_issue_reference(self):
        """Test formatting an issue number."""
        assert format_issue_reference(156) == "#156"

    def test_format_issue_reference_single_digit(self):
        """Test formatting a single digit issue number."""
        assert format_issue_reference(1) == "#1"

    def test_format_issue_reference_large_number(self):
        """Test formatting a large issue number."""
        assert format_issue_reference(99999) == "#99999"


class TestParseIssueReference:
    """Tests for parse_issue_reference function."""

    def test_parse_issue_reference(self):
        """Test parsing a valid issue reference."""
        assert parse_issue_reference("#156") == 156

    def test_parse_issue_reference_single_digit(self):
        """Test parsing a single digit issue reference."""
        assert parse_issue_reference("#1") == 1

    def test_parse_issue_reference_no_hash(self):
        """Test parsing a reference without hash returns None."""
        assert parse_issue_reference("156") is None

    def test_parse_issue_reference_empty(self):
        """Test parsing an empty reference returns None."""
        assert parse_issue_reference("") is None

    def test_parse_issue_reference_invalid(self):
        """Test parsing an invalid reference returns None."""
        assert parse_issue_reference("#abc") is None

    def test_parse_issue_reference_hash_only(self):
        """Test parsing just a hash returns None."""
        assert parse_issue_reference("#") is None
