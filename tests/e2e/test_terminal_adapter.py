"""Terminal adapter config checks."""

import logging
import os

import pytest

logger = logging.getLogger(__name__)


@pytest.mark.e2e
@pytest.mark.timeout(30)
class TestTerminalAdapterConfig:
    """Test terminal adapter configuration."""

    @pytest.mark.gh_activity_limit(test_gh_activity_limit=10, system_gh_activity_limit=10)
    def test_terminal_adapter_mode_configured(
        self,
        e2e_orchestrator,
        e2e_ui_mode: str,
    ):
        """Verify the terminal adapter mode is properly configured."""
        expected_mode = os.environ.get("E2E_UI_MODE", "tmux")
        assert e2e_ui_mode == expected_mode, f"Expected {expected_mode}, got {e2e_ui_mode}"
        logger.info("Terminal adapter mode: %s", e2e_ui_mode)

        # Verify orchestrator is running with this mode
        assert e2e_orchestrator.is_running(), "Orchestrator should be running"
        logger.info("Orchestrator running in %s mode", e2e_ui_mode)
