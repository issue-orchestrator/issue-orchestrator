"""Tests for E2E role detection logic."""

import pytest

from issue_orchestrator.infra.config import E2EConfig
from issue_orchestrator.infra.e2e_slot_policy import get_e2e_role


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

    def test_auto_single_instance(self) -> None:
        """Auto mode, single instance is executor."""
        config = E2EConfig(enabled=True, role="auto")
        # No instance_id means single-instance mode
        assert get_e2e_role(config, instance_id=None) == "executor"

    def test_auto_first_instance(self) -> None:
        """Auto mode, orchestrator-1 is executor."""
        config = E2EConfig(enabled=True, role="auto")
        assert get_e2e_role(config, instance_id="orchestrator-1") == "executor"

    def test_auto_other_instances(self) -> None:
        """Auto mode, orchestrator-2+ are readers."""
        config = E2EConfig(enabled=True, role="auto")
        assert get_e2e_role(config, instance_id="orchestrator-2") == "reader"
        assert get_e2e_role(config, instance_id="orchestrator-3") == "reader"

    def test_explicit_role_ignores_instance_id(self) -> None:
        """Explicit role is used regardless of instance_id."""
        config = E2EConfig(enabled=True, role="executor")
        # Even orchestrator-2 gets executor if explicitly set
        assert get_e2e_role(config, instance_id="orchestrator-2") == "executor"

    def test_explicit_reader_for_all_instances(self) -> None:
        """Explicit reader role applies to all instances."""
        config = E2EConfig(enabled=True, role="reader")
        assert get_e2e_role(config, instance_id=None) == "reader"
        assert get_e2e_role(config, instance_id="orchestrator-1") == "reader"
        assert get_e2e_role(config, instance_id="orchestrator-2") == "reader"


class TestE2EConfigRoleParsing:
    """Tests for E2E role config parsing."""

    def test_default_role_is_auto(self) -> None:
        """Default role should be 'auto'."""
        config = E2EConfig()
        assert config.role == "auto"
