"""Unit tests for ClaimGate."""

import pytest

from issue_orchestrator.control.claim_gate import ClaimGate, ClaimLostError


class MockClaimManager:
    """Mock ClaimManager for testing."""

    def __init__(self, is_winner: bool = True):
        self._is_winner = is_winner
        self.check_winner_calls: list[tuple[int, str]] = []

    def check_winner(self, issue_number: int, lease_id: str) -> bool:
        self.check_winner_calls.append((issue_number, lease_id))
        return self._is_winner


class MockEventSink:
    """Mock event sink for testing."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, event):
        self.events.append((event.event_type, event.data))


class TestVerifyBeforeWrite:
    """Tests for ClaimGate.verify_before_write."""

    def test_allows_write_when_winner(self):
        """Returns True when we are the current winner."""
        claim_manager = MockClaimManager(is_winner=True)
        events = MockEventSink()
        gate = ClaimGate(claim_manager, events)  # type: ignore

        result = gate.verify_before_write(
            issue_number=42,
            lease_id="test-lease",
            operation="push",
        )

        assert result is True
        assert len(claim_manager.check_winner_calls) == 1
        assert claim_manager.check_winner_calls[0] == (42, "test-lease")

    def test_blocks_write_when_not_winner(self):
        """Returns False when we are not the current winner."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        gate = ClaimGate(claim_manager, events)  # type: ignore

        result = gate.verify_before_write(
            issue_number=42,
            lease_id="test-lease",
            operation="push",
        )

        assert result is False
        assert len(claim_manager.check_winner_calls) == 1

    def test_allows_write_when_no_lease_id(self):
        """Returns True when lease_id is None (no claim system)."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        gate = ClaimGate(claim_manager, events)  # type: ignore

        result = gate.verify_before_write(
            issue_number=42,
            lease_id=None,  # No claim system active
            operation="push",
        )

        assert result is True
        # Should not have called check_winner
        assert len(claim_manager.check_winner_calls) == 0

    def test_emits_event_when_claim_lost(self):
        """Emits CLAIM_LOST_BEFORE_WRITE event when claim is lost."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        gate = ClaimGate(claim_manager, events)  # type: ignore

        result = gate.verify_before_write(
            issue_number=42,
            lease_id="test-lease",
            operation="add_label",
        )

        assert result is False
        assert len(events.events) == 1
        event_type, data = events.events[0]
        # Check event type by name or value
        assert "lost_before_write" in str(event_type).lower()
        assert data["issue_number"] == 42
        assert data["lease_id"] == "test-lease"
        assert data["operation"] == "add_label"

    def test_no_event_when_winner(self):
        """Does not emit event when we are the winner."""
        claim_manager = MockClaimManager(is_winner=True)
        events = MockEventSink()
        gate = ClaimGate(claim_manager, events)  # type: ignore

        result = gate.verify_before_write(
            issue_number=42,
            lease_id="test-lease",
            operation="push",
        )

        assert result is True
        assert len(events.events) == 0


class TestVerifyOrRaise:
    """Tests for ClaimGate.verify_or_raise."""

    def test_does_not_raise_when_winner(self):
        """Does not raise when we are the current winner."""
        claim_manager = MockClaimManager(is_winner=True)
        events = MockEventSink()
        gate = ClaimGate(claim_manager, events)  # type: ignore

        # Should not raise
        gate.verify_or_raise(
            issue_number=42,
            lease_id="test-lease",
            operation="push",
        )

    def test_raises_claim_lost_error_when_not_winner(self):
        """Raises ClaimLostError when we are not the winner."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        gate = ClaimGate(claim_manager, events)  # type: ignore

        with pytest.raises(ClaimLostError) as exc_info:
            gate.verify_or_raise(
                issue_number=42,
                lease_id="test-lease",
                operation="push",
            )

        assert exc_info.value.issue_number == 42
        assert exc_info.value.operation == "push"
        assert "42" in str(exc_info.value)
        assert "push" in str(exc_info.value)

    def test_does_not_raise_when_no_lease_id(self):
        """Does not raise when lease_id is None."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        gate = ClaimGate(claim_manager, events)  # type: ignore

        # Should not raise even though is_winner=False
        gate.verify_or_raise(
            issue_number=42,
            lease_id=None,
            operation="push",
        )


class TestClaimLostError:
    """Tests for ClaimLostError exception."""

    def test_error_contains_issue_number(self):
        """Error message contains issue number."""
        error = ClaimLostError(42, "push")
        assert "42" in str(error)

    def test_error_contains_operation(self):
        """Error message contains operation."""
        error = ClaimLostError(42, "add_label")
        assert "add_label" in str(error)

    def test_error_attributes(self):
        """Error has correct attributes."""
        error = ClaimLostError(42, "push")
        assert error.issue_number == 42
        assert error.operation == "push"
