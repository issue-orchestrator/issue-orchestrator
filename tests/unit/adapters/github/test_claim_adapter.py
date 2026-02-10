"""Unit tests for adapters/github/claim_adapter.py."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.adapters.github.claim_adapter import GitHubClaimAdapter
from issue_orchestrator.domain.claim import ClaimState
from issue_orchestrator.domain.lease_config import LeaseConfig

class MockHttpClient:
    """Mock GitHub HTTP client for testing."""

    def __init__(self):
        self.comments: list[dict] = []
        self.add_comment_calls: list[tuple[int, str]] = []

    def add_comment(self, issue_number: int, body: str) -> str:
        self.add_comment_calls.append((issue_number, body))
        self.comments.append({"body": body})
        return f"https://github.com/test/repo/issues/{issue_number}#comment"

    def get_issue_comments(self, issue_number: int) -> list[dict]:
        return self.comments

class MockLabelAdapter:
    """Mock label adapter for testing."""

    def __init__(self):
        self.labels: dict[int, set[str]] = {}
        self.add_label_calls: list[tuple[int, str]] = []
        self.remove_label_calls: list[tuple[int, str]] = []

    def add_label(self, issue_number: int, label: str) -> None:
        self.add_label_calls.append((issue_number, label))
        self.labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.remove_label_calls.append((issue_number, label))
        self.labels.get(issue_number, set()).discard(label)

@pytest.fixture
def mock_client():
    return MockHttpClient()

@pytest.fixture
def mock_labels():
    return MockLabelAdapter()

@pytest.fixture
def adapter(mock_client, mock_labels):
    return GitHubClaimAdapter(
        client=mock_client,
        claimant_id="test-orchestrator",
        config=LeaseConfig.for_testing(),
        label_adapter=mock_labels,
    )

class TestAttemptClaim:
    """Tests for attempt_claim method."""

    def test_creates_claim_comment(self, adapter, mock_client):
        """attempt_claim posts a claim comment."""
        result = adapter.attempt_claim(issue_number=42)

        assert result.success is True
        assert result.lease_id is not None
        assert len(mock_client.add_comment_calls) == 1
        issue_num, body = mock_client.add_comment_calls[0]
        assert issue_num == 42
        assert "io-claim" in body
        assert "test-orchestrator" in body

    def test_adds_claim_label(self, adapter, mock_labels):
        """attempt_claim adds the io:claimed label."""
        adapter.attempt_claim(issue_number=42)

        assert len(mock_labels.add_label_calls) == 1
        issue_num, label = mock_labels.add_label_calls[0]
        assert issue_num == 42
        assert label == "io:claimed"

    def test_generates_unique_lease_id(self, adapter):
        """Each claim gets a unique lease_id."""
        result1 = adapter.attempt_claim(issue_number=42)
        result2 = adapter.attempt_claim(issue_number=43)

        assert result1.lease_id != result2.lease_id

    def test_result_state_is_claiming(self, adapter):
        """Result state is CLAIMING (not yet converged)."""
        result = adapter.attempt_claim(issue_number=42)

        assert result.state == ClaimState.CLAIMING

    def test_returns_failure_on_exception(self, mock_labels):
        """Returns failed result if posting comment fails."""
        mock_client = MockHttpClient()
        mock_client.add_comment = MagicMock(side_effect=Exception("Network error"))

        adapter = GitHubClaimAdapter(
            client=mock_client,  # type: ignore - Union type narrowing limitation
            claimant_id="test-orchestrator",
            config=LeaseConfig.for_testing(),
            label_adapter=mock_labels,
        )

        result = adapter.attempt_claim(issue_number=42)

        assert result.success is False
        assert result.state == ClaimState.UNCLAIMED
        assert "Network error" in result.error  # type: ignore - Union type narrowing limitation
class TestReleaseClaim:
    """Tests for release_claim method."""

    def test_removes_claim_label(self, adapter, mock_labels):
        """release_claim removes the io:claimed label."""
        # First claim
        adapter.attempt_claim(issue_number=42)

        # Then release
        adapter.release_claim(issue_number=42, lease_id="test-lease")

        assert len(mock_labels.remove_label_calls) == 1
        issue_num, label = mock_labels.remove_label_calls[0]
        assert issue_num == 42
        assert label == "io:claimed"

class TestCheckWinner:
    """Tests for check_winner method."""

    def test_returns_true_when_winner(self, adapter, mock_client):
        """Returns True when we are the current winner."""
        # Post our claim
        result = adapter.attempt_claim(issue_number=42)

        # Check if we're winner
        is_winner = adapter.check_winner(
            issue_number=42, lease_id=result.lease_id
        )

        assert is_winner is True

    def test_returns_false_when_not_winner(self, adapter, mock_client):
        """Returns False when someone else's claim wins."""
        # Our claim first (will have current epoch ms as priority)
        result = adapter.attempt_claim(issue_number=42)

        # Manually add another orchestrator's claim with higher priority
        # Use a priority higher than what we just got
        now = datetime.now()
        higher_priority = int(now.timestamp() * 1000) + 1000000  # 1000 seconds in future
        other_claim = f"""```io-claim
lease_id: other-lease-{higher_priority}
claimant: other-orchestrator
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {higher_priority}
```"""
        mock_client.comments.append({"body": other_claim})

        # We should not be winner (other has higher priority)
        is_winner = adapter.check_winner(
            issue_number=42, lease_id=result.lease_id
        )

        assert is_winner is False

class TestRenewClaim:
    """Tests for renew_claim method."""

    def test_renews_when_we_are_winner(self, adapter, mock_client):
        """renew_claim succeeds when we own the claim."""
        # First claim
        result = adapter.attempt_claim(issue_number=42)

        # Renew
        success = adapter.renew_claim(issue_number=42, lease_id=result.lease_id)

        assert success is True
        # Should have posted a second comment (renewal)
        assert len(mock_client.add_comment_calls) == 2

    def test_fails_when_not_winner(self, adapter, mock_client):
        """renew_claim fails when we don't own the claim."""
        # Add someone else's winning claim with high priority
        now = datetime.now()
        high_priority = int(now.timestamp() * 1000) + 1000000  # 1000 seconds in future
        other_claim = f"""```io-claim
lease_id: other-lease-{high_priority}
claimant: other-orchestrator
started_at: {now.strftime('%Y-%m-%dT%H:%M:%S')}
expires_at: {(now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')}
priority: {high_priority}
```"""
        mock_client.comments.append({"body": other_claim})

        # Try to renew a lease we don't own
        success = adapter.renew_claim(issue_number=42, lease_id="my-fake-lease")

        assert success is False
