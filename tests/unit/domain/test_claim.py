"""Unit tests for domain/claim.py."""

from datetime import datetime, timedelta

import pytest

from issue_orchestrator.domain.claim import Claim, ClaimResult, ClaimState


class TestClaim:
    """Tests for the Claim dataclass."""

    def test_claim_is_immutable(self):
        """Claim is a frozen dataclass."""
        claim = Claim(
            lease_id="abc123",
            claimant="orchestrator-a",
            issue_number=42,
            started_at=datetime.now(),
            expires_at=datetime.now() + timedelta(hours=1),
            priority=1000,
        )
        with pytest.raises(AttributeError):
            claim.lease_id = "new-id"  # type: ignore

    def test_is_expired_when_past_expiry(self):
        """is_expired returns True when current time is past expires_at."""
        now = datetime.now()
        claim = Claim(
            lease_id="abc123",
            claimant="orchestrator-a",
            issue_number=42,
            started_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),  # Expired 1 hour ago
            priority=1000,
        )
        assert claim.is_expired() is True

    def test_is_not_expired_when_before_expiry(self):
        """is_expired returns False when current time is before expires_at."""
        now = datetime.now()
        claim = Claim(
            lease_id="abc123",
            claimant="orchestrator-a",
            issue_number=42,
            started_at=now,
            expires_at=now + timedelta(hours=1),  # Expires in 1 hour
            priority=1000,
        )
        assert claim.is_expired() is False

    def test_is_expired_with_explicit_now(self):
        """is_expired accepts explicit now parameter."""
        base_time = datetime(2024, 1, 1, 12, 0, 0)
        claim = Claim(
            lease_id="abc123",
            claimant="orchestrator-a",
            issue_number=42,
            started_at=base_time,
            expires_at=base_time + timedelta(hours=1),
            priority=1000,
        )

        # Before expiry
        assert claim.is_expired(now=base_time + timedelta(minutes=30)) is False

        # After expiry
        assert claim.is_expired(now=base_time + timedelta(hours=2)) is True

    def test_time_until_expiry_seconds(self):
        """time_until_expiry_seconds calculates remaining time correctly."""
        base_time = datetime(2024, 1, 1, 12, 0, 0)
        claim = Claim(
            lease_id="abc123",
            claimant="orchestrator-a",
            issue_number=42,
            started_at=base_time,
            expires_at=base_time + timedelta(hours=1),
            priority=1000,
        )

        # 30 minutes before expiry
        remaining = claim.time_until_expiry_seconds(
            now=base_time + timedelta(minutes=30)
        )
        assert remaining == 1800.0  # 30 minutes in seconds

        # 30 minutes after expiry
        remaining = claim.time_until_expiry_seconds(
            now=base_time + timedelta(hours=1, minutes=30)
        )
        assert remaining == -1800.0  # Negative means expired

    def test_priority_is_epoch_milliseconds(self):
        """Priority should typically be epoch milliseconds for tie-breaking."""
        now = datetime.now()
        priority = int(now.timestamp() * 1000)

        claim = Claim(
            lease_id="abc123",
            claimant="orchestrator-a",
            issue_number=42,
            started_at=now,
            expires_at=now + timedelta(hours=1),
            priority=priority,
        )

        # Priority should be a large integer (epoch ms)
        assert claim.priority > 1000000000000  # After year 2001


class TestClaimState:
    """Tests for the ClaimState enum."""

    def test_all_states_exist(self):
        """All expected states are defined."""
        assert ClaimState.UNCLAIMED.value == "unclaimed"
        assert ClaimState.CLAIMING.value == "claiming"
        assert ClaimState.CLAIMED.value == "claimed"
        assert ClaimState.CLAIM_LOST.value == "claim_lost"
        assert ClaimState.CLAIM_EXPIRED.value == "claim_expired"


class TestClaimResult:
    """Tests for the ClaimResult dataclass."""

    def test_claimed_factory(self):
        """ClaimResult.claimed creates successful result."""
        result = ClaimResult.claimed("lease-123")

        assert result.success is True
        assert result.lease_id == "lease-123"
        assert result.state == ClaimState.CLAIMED
        assert result.competing_claims == []
        assert result.error is None

    def test_contested_factory(self):
        """ClaimResult.contested creates contested result."""
        now = datetime.now()
        competing = [
            Claim(
                lease_id="other-lease",
                claimant="other-orchestrator",
                issue_number=42,
                started_at=now,
                expires_at=now + timedelta(hours=1),
                priority=1000,
            )
        ]

        result = ClaimResult.contested("my-lease", competing)

        assert result.success is False
        assert result.lease_id == "my-lease"
        assert result.state == ClaimState.CLAIMING
        assert len(result.competing_claims) == 1
        assert result.competing_claims[0].lease_id == "other-lease"

    def test_lost_factory(self):
        """ClaimResult.lost creates lost result."""
        now = datetime.now()
        winner = Claim(
            lease_id="winner-lease",
            claimant="winner-orchestrator",
            issue_number=42,
            started_at=now,
            expires_at=now + timedelta(hours=1),
            priority=2000,  # Higher priority
        )

        result = ClaimResult.lost("my-lease", winner)

        assert result.success is False
        assert result.lease_id == "my-lease"
        assert result.state == ClaimState.CLAIM_LOST
        assert len(result.competing_claims) == 1
        assert result.competing_claims[0].lease_id == "winner-lease"

    def test_failed_factory(self):
        """ClaimResult.failed creates failed result."""
        result = ClaimResult.failed("Network error")

        assert result.success is False
        assert result.lease_id is None
        assert result.state == ClaimState.UNCLAIMED
        assert result.competing_claims == []
        assert result.error == "Network error"

    def test_claim_result_is_immutable(self):
        """ClaimResult is a frozen dataclass."""
        result = ClaimResult.claimed("lease-123")
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore
