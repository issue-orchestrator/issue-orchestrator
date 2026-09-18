"""Unit tests for the GitHub ref-backed claim adapter."""

from __future__ import annotations

from datetime import datetime, timedelta

from issue_orchestrator.adapters.github.claim_parser import format_claim_comment
from issue_orchestrator.adapters.github.ref_claim_adapter import (
    CLAIM_REF_PREFIX,
    GitHubRefClaimAdapter,
)
from issue_orchestrator.domain.claim import Claim, ClaimState
from issue_orchestrator.domain.lease_config import LeaseConfig

from .fake_git_data import FakeGitHubRefClient


def seed_claim_ref(
    client: FakeGitHubRefClient, issue_number: int, claim: Claim
) -> None:
    """Point an issue's claim ref at an existing claim."""
    client.seed_record(
        f"{CLAIM_REF_PREFIX}/issue-{issue_number}", format_claim_comment(claim)
    )


class FakeLabels:
    def __init__(self) -> None:
        self.added: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []

    def add_label(self, issue_number: int, label: str) -> None:
        self.added.append((issue_number, label))

    def remove_label(self, issue_number: int, label: str) -> None:
        self.removed.append((issue_number, label))


def _adapter(
    client: FakeGitHubRefClient,
    labels: FakeLabels,
    claimant_id: str,
    *,
    lease_seconds: int = 30,
) -> GitHubRefClaimAdapter:
    return GitHubRefClaimAdapter(
        client=client,
        claimant_id=claimant_id,
        config=LeaseConfig(
            lease_seconds=lease_seconds,
            renew_interval_seconds=10,
            convergence_timeout_seconds=0.1,
            convergence_poll_min_ms=1,
            convergence_poll_max_ms=1,
        ),
        label_adapter=labels,
    )


def test_attempt_claim_creates_issue_ref_and_label() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    result = adapter.attempt_claim(issue_number=42)

    assert result.success is True
    assert result.state == ClaimState.CLAIMED
    assert result.lease_id is not None
    assert client.created_refs[0][0] == f"{CLAIM_REF_PREFIX}/issue-42"
    assert labels.added == [(42, "io:claimed")]
    assert adapter.run_convergence(42, result.lease_id) is True


def test_second_adapter_loses_to_active_ref_claim() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter_a = _adapter(client, labels, "orchestrator-a")
    adapter_b = _adapter(client, labels, "orchestrator-b")

    first = adapter_a.attempt_claim(issue_number=42)
    second = adapter_b.attempt_claim(issue_number=42)

    assert first.success is True
    assert second.success is False
    assert second.state == ClaimState.CLAIM_LOST
    assert second.competing_claims[0].claimant == "orchestrator-a"
    assert "already claimed" in str(second.error)
    assert labels.added == [(42, "io:claimed")]


def test_released_claim_can_be_taken_over_by_another_adapter() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter_a = _adapter(client, labels, "orchestrator-a")
    adapter_b = _adapter(client, labels, "orchestrator-b")

    first = adapter_a.attempt_claim(issue_number=42)
    assert first.lease_id is not None
    adapter_a.release_claim(issue_number=42, lease_id=first.lease_id)
    assert client.deleted_refs == [f"{CLAIM_REF_PREFIX}/issue-42"]
    assert client.get_git_ref(f"{CLAIM_REF_PREFIX}/issue-42") is None

    second = adapter_b.attempt_claim(issue_number=42)

    assert second.success is True
    current = adapter_b.get_current_claim(42)
    assert current is not None
    assert current.lease_id == second.lease_id
    assert current.claimant == "orchestrator-b"
    assert labels.removed == [(42, "io:claimed")]


def test_release_ignores_non_matching_lease() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    result = adapter.attempt_claim(issue_number=42)
    adapter.release_claim(issue_number=42, lease_id="someone-else")

    assert result.lease_id is not None
    assert client.deleted_refs == []
    current = adapter.get_current_claim(42)
    assert current is not None
    assert current.lease_id == result.lease_id
    assert labels.removed == []


def test_default_branch_is_cached_for_first_time_claims() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    first = adapter.attempt_claim(issue_number=42)
    second = adapter.attempt_claim(issue_number=43)

    assert first.success is True
    assert second.success is True
    assert client.default_branch_reads == 1


def test_renew_claim_moves_ref_with_non_force_update() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    result = adapter.attempt_claim(issue_number=42)
    assert result.lease_id is not None
    renewed = adapter.renew_claim(issue_number=42, lease_id=result.lease_id)

    assert renewed is True
    assert client.updated_refs[-1][0] == f"{CLAIM_REF_PREFIX}/issue-42"
    assert client.updated_refs[-1][2] is False
    current = adapter.get_current_claim(42)
    assert current is not None
    assert current.lease_id == result.lease_id


def test_renew_claim_returns_false_on_cas_conflict() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    result = adapter.attempt_claim(issue_number=42)
    assert result.lease_id is not None
    client.conflict_updates_remaining = 1

    assert adapter.renew_claim(issue_number=42, lease_id=result.lease_id) is False


def test_expired_claim_is_not_current() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")
    expired_claim = Claim(
        lease_id="lease-expired",
        claimant="orchestrator-a",
        issue_number=42,
        started_at=datetime.now() - timedelta(seconds=10),
        expires_at=datetime.now() - timedelta(seconds=1),
        priority=1,
    )
    seed_claim_ref(client, 42, expired_claim)

    assert adapter.get_current_claim(42) is None
