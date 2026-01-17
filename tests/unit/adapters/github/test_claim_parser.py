"""Unit tests for adapters/github/claim_parser.py."""

from datetime import datetime, timedelta

import pytest

from issue_orchestrator.adapters.github.claim_parser import (
    extract_all_claims,
    format_claim_comment,
    parse_claim_comment,
)
from issue_orchestrator.domain.claim import Claim


class TestParseClaimComment:
    """Tests for parse_claim_comment function."""

    def test_parses_valid_claim(self):
        """Parses a valid io-claim YAML block."""
        body = """Some text before

```io-claim
lease_id: abc123-1705412345
claimant: orchestrator-a
started_at: 2024-01-16T12:00:00
expires_at: 2024-01-16T12:15:00
priority: 1705412345000
```

Some text after
"""
        claim = parse_claim_comment(body, issue_number=42)

        assert claim is not None
        assert claim.lease_id == "abc123-1705412345"
        assert claim.claimant == "orchestrator-a"
        assert claim.issue_number == 42
        assert claim.priority == 1705412345000

    def test_returns_none_for_no_claim_block(self):
        """Returns None when no io-claim block is present."""
        body = "Just a regular comment with no claim."
        claim = parse_claim_comment(body)

        assert claim is None

    def test_returns_none_for_malformed_yaml(self):
        """Returns None when YAML is malformed."""
        body = """```io-claim
this is: not: valid: yaml: at: all
```"""
        claim = parse_claim_comment(body)

        assert claim is None

    def test_returns_none_for_missing_required_fields(self):
        """Returns None when required fields are missing."""
        body = """```io-claim
lease_id: abc123
claimant: orchestrator-a
# Missing started_at, expires_at, priority
```"""
        claim = parse_claim_comment(body)

        assert claim is None

    def test_uses_last_claim_block_if_multiple(self):
        """If multiple claim blocks exist, uses the last one."""
        body = """```io-claim
lease_id: old-lease
claimant: orchestrator-a
started_at: 2024-01-16T10:00:00
expires_at: 2024-01-16T10:15:00
priority: 1000
```

Edit: Updated claim

```io-claim
lease_id: new-lease
claimant: orchestrator-a
started_at: 2024-01-16T12:00:00
expires_at: 2024-01-16T12:15:00
priority: 2000
```
"""
        claim = parse_claim_comment(body, issue_number=42)

        assert claim is not None
        assert claim.lease_id == "new-lease"
        assert claim.priority == 2000

    def test_case_insensitive_block_marker(self):
        """io-claim marker is case-insensitive."""
        body = """```IO-CLAIM
lease_id: abc123
claimant: orchestrator-a
started_at: 2024-01-16T12:00:00
expires_at: 2024-01-16T12:15:00
priority: 1000
```"""
        claim = parse_claim_comment(body, issue_number=42)

        assert claim is not None
        assert claim.lease_id == "abc123"

    def test_handles_datetime_object_from_yaml(self):
        """Handles when YAML parser returns datetime objects."""
        body = """```io-claim
lease_id: abc123
claimant: orchestrator-a
started_at: 2024-01-16T12:00:00
expires_at: 2024-01-16T12:15:00
priority: 1000
```"""
        claim = parse_claim_comment(body, issue_number=42)

        assert claim is not None
        assert isinstance(claim.started_at, datetime)
        assert isinstance(claim.expires_at, datetime)


class TestFormatClaimComment:
    """Tests for format_claim_comment function."""

    def test_formats_claim_as_yaml_block(self):
        """Formats claim as io-claim YAML block."""
        claim = Claim(
            lease_id="abc123",
            claimant="orchestrator-a",
            issue_number=42,
            started_at=datetime(2024, 1, 16, 12, 0, 0),
            expires_at=datetime(2024, 1, 16, 12, 15, 0),
            priority=1705412345000,
        )

        body = format_claim_comment(claim)

        assert body.startswith("```io-claim")
        assert body.endswith("```")
        assert "lease_id: abc123" in body
        assert "claimant: orchestrator-a" in body
        assert "priority: 1705412345000" in body

    def test_roundtrip_preserves_claim(self):
        """Format and parse roundtrip preserves claim data."""
        original = Claim(
            lease_id="roundtrip-test",
            claimant="orchestrator-b",
            issue_number=99,
            started_at=datetime(2024, 6, 15, 10, 30, 0),
            expires_at=datetime(2024, 6, 15, 10, 45, 0),
            priority=1718444200000,
        )

        body = format_claim_comment(original)
        parsed = parse_claim_comment(body, issue_number=99)

        assert parsed is not None
        assert parsed.lease_id == original.lease_id
        assert parsed.claimant == original.claimant
        assert parsed.issue_number == original.issue_number
        assert parsed.priority == original.priority
        # Datetime comparison (may have microsecond differences)
        assert abs((parsed.started_at - original.started_at).total_seconds()) < 1
        assert abs((parsed.expires_at - original.expires_at).total_seconds()) < 1


class TestExtractAllClaims:
    """Tests for extract_all_claims function."""

    def test_extracts_claims_from_multiple_comments(self):
        """Extracts claims from a list of comments."""
        comments = [
            {"body": "Regular comment, no claim"},
            {
                "body": """```io-claim
lease_id: claim-1
claimant: orch-a
started_at: 2024-01-16T12:00:00
expires_at: 2024-01-16T12:15:00
priority: 1000
```"""
            },
            {"body": "Another regular comment"},
            {
                "body": """```io-claim
lease_id: claim-2
claimant: orch-b
started_at: 2024-01-16T12:05:00
expires_at: 2024-01-16T12:20:00
priority: 2000
```"""
            },
        ]

        claims = extract_all_claims(comments, issue_number=42)

        assert len(claims) == 2
        assert claims[0].lease_id == "claim-1"
        assert claims[1].lease_id == "claim-2"

    def test_skips_comments_without_claims(self):
        """Skips comments that don't have valid claims."""
        comments = [
            {"body": "No claim here"},
            {"body": "```python\nprint('not a claim')\n```"},
            {"body": None},  # Edge case: None body
            {},  # Edge case: missing body key
        ]

        claims = extract_all_claims(comments, issue_number=42)

        assert len(claims) == 0

    def test_empty_comment_list(self):
        """Handles empty comment list."""
        claims = extract_all_claims([], issue_number=42)

        assert claims == []
