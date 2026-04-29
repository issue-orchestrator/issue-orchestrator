"""Unit tests for the disabled-claims ClaimManager implementation."""

from issue_orchestrator.domain.claim import ClaimState
from issue_orchestrator.ports.claim_manager import NullClaimManager


def test_null_claim_manager_claims_without_external_state() -> None:
    manager = NullClaimManager()

    result = manager.attempt_claim(42)

    assert result.success is True
    assert result.state == ClaimState.CLAIMED
    assert result.lease_id == "null-claim-42"
    assert result.competing_claims == []


def test_null_claim_manager_liveness_methods_are_noops() -> None:
    manager = NullClaimManager()

    assert manager.run_convergence(42, "lease") is True
    assert manager.renew_claim(42, "lease") is True
    assert manager.check_winner(42, "lease") is True
    assert manager.get_current_claim(42) is None

    manager.release_claim(42, "lease")
