"""Unit tests for domain/lease_config.py."""

import pytest

from issue_orchestrator.domain.lease_config import ClaimConfig, LeaseConfig


class TestLeaseConfig:
    """Tests for the LeaseConfig dataclass."""

    def test_default_values(self):
        """LeaseConfig has sensible production defaults."""
        config = LeaseConfig()

        assert config.lease_seconds == 900  # 15 minutes
        assert config.renew_interval_seconds == 300  # 5 minutes
        assert config.convergence_timeout_seconds == 5.0
        assert config.convergence_poll_min_ms == 250
        assert config.convergence_poll_max_ms == 500
        assert config.convergence_max_polls == 15

    def test_for_testing_factory(self):
        """LeaseConfig.for_testing creates config with short times."""
        config = LeaseConfig.for_testing()

        assert config.lease_seconds == 30  # 30 seconds
        assert config.renew_interval_seconds == 10  # 10 seconds
        assert config.convergence_timeout_seconds == 3.0
        assert config.convergence_poll_min_ms == 100
        assert config.convergence_poll_max_ms == 200
        assert config.convergence_max_polls == 15

    def test_renewal_trigger_threshold(self):
        """renewal_trigger_threshold returns when to trigger renewal."""
        config = LeaseConfig()

        # With 15-min lease and 5-min renew interval, trigger when 10 min remain
        threshold = config.renewal_trigger_threshold()
        assert threshold == 600  # 900 - 300

    def test_renewal_trigger_threshold_for_testing(self):
        """renewal_trigger_threshold works for test config."""
        config = LeaseConfig.for_testing()

        # With 30s lease and 10s renew interval, trigger when 20s remain
        threshold = config.renewal_trigger_threshold()
        assert threshold == 20  # 30 - 10

    def test_is_immutable(self):
        """LeaseConfig is a frozen dataclass."""
        config = LeaseConfig()
        with pytest.raises(AttributeError):
            config.lease_seconds = 100  # type: ignore


class TestClaimConfig:
    """Tests for the ClaimConfig dataclass."""

    def test_default_values(self):
        """ClaimConfig defaults to disabled."""
        config = ClaimConfig()

        assert config.enabled is False
        assert isinstance(config.lease, LeaseConfig)
        assert config.claimant_id == ""

    def test_enabled_with_claimant_id(self):
        """ClaimConfig can be enabled with explicit claimant_id."""
        config = ClaimConfig(
            enabled=True,
            claimant_id="my-orchestrator",
        )

        assert config.enabled is True
        assert config.claimant_id == "my-orchestrator"

    def test_auto_generates_claimant_id_when_enabled(self):
        """ClaimConfig generates claimant_id if enabled without one."""
        config = ClaimConfig(enabled=True)

        # Should auto-generate hostname-pid format
        assert config.claimant_id != ""
        assert "-" in config.claimant_id  # hostname-pid format

    def test_disabled_does_not_generate_claimant_id(self):
        """ClaimConfig does not generate claimant_id when disabled."""
        config = ClaimConfig(enabled=False)

        assert config.claimant_id == ""

    def test_custom_lease_config(self):
        """ClaimConfig accepts custom LeaseConfig."""
        lease = LeaseConfig.for_testing()
        config = ClaimConfig(
            enabled=True,
            lease=lease,
            claimant_id="test-orchestrator",
        )

        assert config.lease.lease_seconds == 30
        assert config.lease.renew_interval_seconds == 10
