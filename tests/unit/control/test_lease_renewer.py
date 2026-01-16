"""Unit tests for LeaseRenewer."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.lease_renewer import LeaseRenewer
from issue_orchestrator.domain.lease_config import LeaseConfig
from issue_orchestrator.domain.models import Issue, Session, SessionKey, TaskKind


class MockClaimManager:
    """Mock ClaimManager for testing."""

    def __init__(self, renew_success: bool = True, is_winner: bool = True):
        self._renew_success = renew_success
        self._is_winner = is_winner
        self.renew_claim_calls: list[tuple[int, str]] = []
        self.check_winner_calls: list[tuple[int, str]] = []

    def renew_claim(self, issue_number: int, lease_id: str) -> bool:
        self.renew_claim_calls.append((issue_number, lease_id))
        return self._renew_success

    def check_winner(self, issue_number: int, lease_id: str) -> bool:
        self.check_winner_calls.append((issue_number, lease_id))
        return self._is_winner


class MockEventSink:
    """Mock event sink for testing."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, event):
        self.events.append((event.event_type, event.data))


def create_test_session(
    issue_number: int = 42,
    lease_id: str | None = "test-lease",
    expires_in_seconds: float = 300,
) -> Session:
    """Create a test session with configurable lease expiry."""
    issue_key = MagicMock()
    issue_key.stable_id.return_value = f"issue-{issue_number}"
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)

    now = datetime.now()
    lease_acquired_at = now - timedelta(minutes=5)
    lease_expires_at = now + timedelta(seconds=expires_in_seconds) if lease_id else None

    return Session(
        key=session_key,
        issue=Issue(number=issue_number, title=f"Issue #{issue_number}", labels=["test"]),
        agent_config=MagicMock(command="test"),
        terminal_id=f"issue-{issue_number}",
        worktree_path=Path("/tmp/worktree"),
        branch_name="test-branch",
        completion_path="completion.json",
        agent_label="test-agent",
        lease_id=lease_id,
        lease_acquired_at=lease_acquired_at if lease_id else None,
        lease_expires_at=lease_expires_at,
    )


class TestCheckRenewals:
    """Tests for LeaseRenewer.check_renewals."""

    @pytest.fixture
    def config(self):
        return LeaseConfig(
            lease_seconds=900,  # 15 minutes
            renew_interval_seconds=300,  # 5 minutes
        )

    def test_renews_when_within_threshold(self, config):
        """Renews lease when within renewal threshold."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Create session expiring soon (within 300s threshold)
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,  # 200s remaining < 600s threshold
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        assert len(claim_manager.renew_claim_calls) == 1
        assert claim_manager.renew_claim_calls[0] == (42, "test-lease")

    def test_skips_renewal_when_not_needed(self, config):
        """Does not renew when plenty of time remaining."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Create session with lots of time remaining
        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=800,  # 800s remaining > 600s threshold
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        assert len(claim_manager.renew_claim_calls) == 0

    def test_returns_lost_sessions_on_renewal_failure(self, config):
        """Returns sessions that fail renewal."""
        claim_manager = MockClaimManager(renew_success=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,  # Within renewal threshold
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 1
        assert lost_sessions[0] == session

    def test_updates_expires_at_on_success(self, config):
        """Updates session.lease_expires_at on successful renewal."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,
        )
        old_expiry = session.lease_expires_at

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        # Expiry should have been extended
        assert session.lease_expires_at > old_expiry

    def test_skips_sessions_without_lease(self, config):
        """Skips sessions that don't have a lease_id."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id=None,  # No lease
        )

        lost_sessions = renewer.check_renewals([session])

        assert len(lost_sessions) == 0
        assert len(claim_manager.renew_claim_calls) == 0

    def test_handles_multiple_sessions(self, config):
        """Handles multiple sessions correctly."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        sessions = [
            create_test_session(42, "lease-a", 200),  # Needs renewal
            create_test_session(43, "lease-b", 800),  # No renewal needed
            create_test_session(44, "lease-c", 100),  # Needs renewal
        ]

        lost_sessions = renewer.check_renewals(sessions)

        assert len(lost_sessions) == 0
        # Only sessions 42 and 44 should be renewed
        assert len(claim_manager.renew_claim_calls) == 2
        issue_numbers = [call[0] for call in claim_manager.renew_claim_calls]
        assert 42 in issue_numbers
        assert 44 in issue_numbers
        assert 43 not in issue_numbers

    def test_emits_renewal_event(self, config):
        """Emits CLAIM_RENEWED event on successful renewal."""
        claim_manager = MockClaimManager(renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,
        )

        renewer.check_renewals([session])

        assert len(events.events) == 1
        event_type, data = events.events[0]
        assert "renewed" in str(event_type).lower()
        assert data["issue_number"] == 42
        assert data["lease_id"] == "test-lease"

    def test_emits_claim_lost_event_on_failure(self, config):
        """Emits CLAIM_LOST event when renewal fails."""
        claim_manager = MockClaimManager(renew_success=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(
            issue_number=42,
            lease_id="test-lease",
            expires_in_seconds=200,
        )

        renewer.check_renewals([session])

        assert len(events.events) == 1
        event_type, data = events.events[0]
        assert "lost" in str(event_type).lower()
        assert data["issue_number"] == 42
        assert data["reason"] == "renewal_failed"


class TestCheckSingleSession:
    """Tests for LeaseRenewer.check_single_session."""

    @pytest.fixture
    def config(self):
        return LeaseConfig.for_testing()

    def test_returns_true_when_winner(self, config):
        """Returns True when session is still the claim winner."""
        claim_manager = MockClaimManager(is_winner=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(issue_number=42, lease_id="test-lease")

        result = renewer.check_single_session(session)

        assert result is True
        assert len(claim_manager.check_winner_calls) == 1

    def test_returns_false_when_not_winner(self, config):
        """Returns False when session has lost the claim."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(issue_number=42, lease_id="test-lease")

        result = renewer.check_single_session(session)

        assert result is False
        assert len(claim_manager.check_winner_calls) == 1

    def test_returns_true_when_no_lease(self, config):
        """Returns True when session has no lease (no claim system)."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(issue_number=42, lease_id=None)

        result = renewer.check_single_session(session)

        assert result is True
        assert len(claim_manager.check_winner_calls) == 0

    def test_emits_event_when_claim_lost(self, config):
        """Emits CLAIM_LOST event when claim check fails."""
        claim_manager = MockClaimManager(is_winner=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_test_session(issue_number=42, lease_id="test-lease")

        renewer.check_single_session(session)

        assert len(events.events) == 1
        event_type, data = events.events[0]
        assert "lost" in str(event_type).lower()
        assert data["reason"] == "on_demand_check"
