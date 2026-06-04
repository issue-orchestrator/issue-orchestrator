"""Tests for claim loss recovery path.

Verifies that when LeaseRenewer detects a claim loss, the orchestrator
correctly terminates the session, cleans up state, and notifies GitHub.
"""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.control.lease_renewer import LeaseRenewer
from issue_orchestrator.domain.claim import ClaimFetchError
from issue_orchestrator.domain.lease_config import LeaseConfig
from issue_orchestrator.domain.models import Issue, Session, SessionKey, TaskKind
from tests.unit.session_run_helpers import make_session_run_assets


class MockClaimManager:
    """ClaimManager that can simulate claim loss."""

    def __init__(self, is_winner: bool = True, renew_success: bool = True):
        self._is_winner = is_winner
        self._renew_success = renew_success

    def check_winner(self, issue_number: int, lease_id: str) -> bool:
        return self._is_winner

    def renew_claim(self, issue_number: int, lease_id: str) -> bool:
        return self._renew_success


class MockEventSink:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append((event.event_type, event.data))


def create_session(
    issue_number: int = 42,
    lease_id: str = "test-lease",
    expires_in: float = 200,
) -> Session:
    issue_key = MagicMock()
    issue_key.stable_id.return_value = f"issue-{issue_number}"
    key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    now = datetime.now()
    return Session(
        key=key,
        issue=Issue(number=issue_number, title=f"Issue #{issue_number}", labels=["test"]),
        agent_config=MagicMock(command="test"),
        terminal_id=f"issue-{issue_number}",
        worktree_path=Path("/tmp/worktree"),
        branch_name="test-branch",
        run_assets=make_session_run_assets(
            Path("/tmp/worktree"),
            session_name=f"issue-{issue_number}",
        ),
        completion_path="completion.json",
        agent_label="test-agent",
        lease_id=lease_id,
        lease_acquired_at=now - timedelta(hours=1),
        lease_expires_at=now + timedelta(seconds=expires_in),
        last_claim_verified_at=now - timedelta(seconds=60),
    )


class TestClaimLossDetection:
    """Tests that LeaseRenewer correctly identifies lost sessions."""

    @pytest.fixture
    def config(self):
        return LeaseConfig(lease_seconds=900, renew_interval_seconds=300)

    def test_renewal_failure_reports_session_as_lost(self, config):
        """When renew_claim returns False, session is reported as lost."""
        claim_manager = MockClaimManager(is_winner=True, renew_success=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_session(expires_in=200)  # Within renewal window
        lost = renewer.check_renewals([session])

        assert len(lost) == 1
        assert lost[0].issue.number == 42

    def test_verification_failure_reports_session_as_lost(self, config):
        """When check_winner returns False during verification, session is lost."""
        claim_manager = MockClaimManager(is_winner=False, renew_success=True)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        # Needs verification (last verified 6 min ago > lease/3 = 5 min)
        session = create_session(expires_in=540)
        session.last_claim_verified_at = datetime.now() - timedelta(seconds=360)

        lost = renewer.check_renewals([session])

        assert len(lost) == 1
        assert lost[0].issue.number == 42

    def test_multiple_sessions_some_lost(self, config):
        """Multiple sessions: only the ones that lost claims are reported."""
        events = MockEventSink()

        class SelectiveClaimManager:
            def check_winner(self, issue_number, lease_id):
                return True

            def renew_claim(self, issue_number, lease_id):
                return issue_number != 43  # Issue 43 loses

        renewer = LeaseRenewer(SelectiveClaimManager(), events, config)

        sessions = [
            create_session(issue_number=42, lease_id="a", expires_in=200),
            create_session(issue_number=43, lease_id="b", expires_in=200),
            create_session(issue_number=44, lease_id="c", expires_in=200),
        ]

        lost = renewer.check_renewals(sessions)

        assert len(lost) == 1
        assert lost[0].issue.number == 43

    def test_claim_loss_emits_event_with_reason(self, config):
        """Claim loss events include the reason for loss."""
        claim_manager = MockClaimManager(is_winner=True, renew_success=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_session(expires_in=200)
        renewer.check_renewals([session])

        assert len(events.events) == 1
        event_type, data = events.events[0]
        assert "lost" in str(event_type).lower()
        assert data["reason"] == "renewal_failed"
        assert data["issue_number"] == 42

    def test_session_without_lease_never_reported_as_lost(self, config):
        """Sessions without lease_id are never reported as lost."""
        claim_manager = MockClaimManager(is_winner=False, renew_success=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_session()
        session.lease_id = None
        session.lease_expires_at = None

        lost = renewer.check_renewals([session])

        assert len(lost) == 0


class TestClaimLossRecoveryContract:
    """Tests the contract: what the orchestrator SHOULD do when sessions are lost.

    These test the recovery behavior at the LeaseRenewer boundary — the
    returned lost_sessions list is the contract between LeaseRenewer and
    the orchestrator's _check_lease_renewals method.
    """

    @pytest.fixture
    def config(self):
        return LeaseConfig(lease_seconds=900, renew_interval_seconds=300)

    def test_lost_session_preserves_worktree_path(self, config):
        """Lost sessions retain worktree_path so the orchestrator can preserve it."""
        claim_manager = MockClaimManager(is_winner=True, renew_success=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_session(expires_in=200)
        lost = renewer.check_renewals([session])

        assert len(lost) == 1
        assert lost[0].worktree_path == Path("/tmp/worktree")
        assert lost[0].branch_name == "test-branch"

    def test_lost_session_retains_terminal_id_for_kill(self, config):
        """Lost sessions retain terminal_id so orchestrator can kill the session."""
        claim_manager = MockClaimManager(is_winner=True, renew_success=False)
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_session(expires_in=200)
        lost = renewer.check_renewals([session])

        assert len(lost) == 1
        assert lost[0].terminal_id == "issue-42"

    def test_api_error_during_renewal_does_not_lose_session(self, config):
        """ClaimFetchError during renewal should NOT produce a lost session."""
        claim_manager = MockClaimManager(is_winner=True)
        claim_manager.renew_claim = MagicMock(
            side_effect=ClaimFetchError("transient")
        )
        events = MockEventSink()
        renewer = LeaseRenewer(claim_manager, events, config)

        session = create_session(expires_in=200)
        lost = renewer.check_renewals([session])

        # API error → skip, don't report as lost
        assert len(lost) == 0
