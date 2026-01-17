"""Tests for E2E role detection logic."""

import pytest

from issue_orchestrator.infra.config import E2EConfig
from issue_orchestrator.infra.e2e_runner import get_e2e_role


class TestGetE2ERole:
    """Tests for get_e2e_role function."""

    def test_explicit_executor_role(self) -> None:
        """Explicit role=executor overrides auto-detection."""
        config = E2EConfig(enabled=True, role="executor")
        assert get_e2e_role(config) == "executor"

    def test_explicit_reader_role(self) -> None:
        """Explicit role=reader overrides auto-detection."""
        config = E2EConfig(enabled=True, role="reader")
        assert get_e2e_role(config) == "reader"

    def test_explicit_disabled_role(self) -> None:
        """Explicit role=disabled overrides auto-detection."""
        config = E2EConfig(enabled=True, role="disabled")
        assert get_e2e_role(config) == "disabled"

    def test_auto_with_executor_claimant_match(self) -> None:
        """Auto mode with matching executor_claimant returns executor."""
        config = E2EConfig(
            enabled=True,
            role="auto",
            executor_claimant="e2e-server",
        )
        assert get_e2e_role(config, claimant_id="e2e-server") == "executor"

    def test_auto_with_executor_claimant_no_match(self) -> None:
        """Auto mode with non-matching claimant_id returns reader."""
        config = E2EConfig(
            enabled=True,
            role="auto",
            executor_claimant="e2e-server",
        )
        assert get_e2e_role(config, claimant_id="alice-mbp") == "reader"

    def test_auto_with_executor_claimant_no_claimant_id(self) -> None:
        """Auto mode with executor_claimant but no claimant_id returns reader."""
        config = E2EConfig(
            enabled=True,
            role="auto",
            executor_claimant="e2e-server",
        )
        assert get_e2e_role(config, claimant_id=None) == "reader"

    def test_auto_no_executor_claimant_single_instance(self) -> None:
        """Auto mode without executor_claimant, single instance is executor."""
        config = E2EConfig(enabled=True, role="auto")
        # No instance_id means single-instance mode
        assert get_e2e_role(config, instance_id=None) == "executor"

    def test_auto_no_executor_claimant_first_instance(self) -> None:
        """Auto mode without executor_claimant, orchestrator-1 is executor."""
        config = E2EConfig(enabled=True, role="auto")
        assert get_e2e_role(config, instance_id="orchestrator-1") == "executor"

    def test_auto_no_executor_claimant_other_instance(self) -> None:
        """Auto mode without executor_claimant, orchestrator-2+ are readers."""
        config = E2EConfig(enabled=True, role="auto")
        assert get_e2e_role(config, instance_id="orchestrator-2") == "reader"
        assert get_e2e_role(config, instance_id="orchestrator-3") == "reader"

    def test_explicit_role_ignores_instance_id(self) -> None:
        """Explicit role is used regardless of instance_id."""
        config = E2EConfig(enabled=True, role="executor")
        # Even orchestrator-2 gets executor if explicitly set
        assert get_e2e_role(config, instance_id="orchestrator-2") == "executor"

    def test_explicit_role_ignores_executor_claimant(self) -> None:
        """Explicit role is used regardless of executor_claimant match."""
        config = E2EConfig(
            enabled=True,
            role="reader",
            executor_claimant="e2e-server",
        )
        # Even matching claimant_id gets reader if role is explicit
        assert get_e2e_role(config, claimant_id="e2e-server") == "reader"


class TestE2EConfigRoleParsing:
    """Tests for E2E role config parsing."""

    def test_default_role_is_auto(self) -> None:
        """Default role should be 'auto'."""
        config = E2EConfig()
        assert config.role == "auto"

    def test_default_executor_claimant_is_none(self) -> None:
        """Default executor_claimant should be None."""
        config = E2EConfig()
        assert config.executor_claimant is None
