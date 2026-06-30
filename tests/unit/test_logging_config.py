"""Tests for centralized logging configuration."""

import logging

import pytest

from issue_orchestrator.infra import logging_config


class TestQuietNoisyThirdPartyLoggers:
    """The orchestrator log must stay readable; chatty network libraries are pinned.

    Regression: the engine entrypoint produced a 318MB log because httpx INFO
    was only silenced by the Control Center. setup_logging() is the shared
    owner, so any entrypoint that configures logging inherits the suppression.
    """

    @pytest.mark.parametrize("noisy_logger", ["httpx", "httpcore"])
    def test_setup_logging_pins_noisy_loggers_to_warning(self, noisy_logger, tmp_path):
        # Start it noisy to prove setup_logging actually lowers it.
        logging.getLogger(noisy_logger).setLevel(logging.INFO)
        logging_config.reset_logging()
        try:
            logging_config.setup_logging(repo_root=tmp_path, level="INFO")
            assert logging.getLogger(noisy_logger).level == logging.WARNING
        finally:
            logging_config.reset_logging()
