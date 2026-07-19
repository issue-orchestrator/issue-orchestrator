"""Unit tests for the on-demand triage CLIs (entrypoints/cli_triage.py).

These pin the command wiring — lock check, main-loop logging, background-E2E
kill switch, and the ``--advise-only`` authority dial — without standing up a
real in-process orchestrator (the driver logic is covered in
``tests/unit/control/test_triage_trigger.py``).
"""

from __future__ import annotations

import argparse
from unittest.mock import Mock, patch

from issue_orchestrator.control.triage_trigger import HealthReviewResult
from issue_orchestrator.entrypoints import cli_triage
from issue_orchestrator.infra.config import Config


def _args(**overrides) -> argparse.Namespace:
    base = {"advise_only": False, "timeout": 1800.0, "config": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def _patches(config: Config):
    """Patch every external boundary cmd_health_review touches except the lock."""
    return (
        patch.object(cli_triage, "load_config", return_value=config),
        patch(
            "issue_orchestrator.infra.logging_config.setup_logging",
            return_value="/tmp/health-review.log",
        ),
    )


class TestCmdHealthReview:
    def test_lock_conflict_returns_1_without_building_orchestrator(self) -> None:
        config = Config()
        load_p, log_p = _patches(config)
        with load_p, log_p, patch(
            "issue_orchestrator.infra.repo_lock.is_locked", return_value=True
        ), patch(
            "issue_orchestrator.infra.repo_lock.read_lock", return_value=None
        ), patch.object(
            cli_triage, "_build_orchestrator"
        ) as build, patch(
            "issue_orchestrator.control.triage_trigger.run_health_review"
        ) as run:
            rc = cli_triage.cmd_health_review(_args())

        assert rc == 1
        build.assert_not_called()  # never built an orchestrator behind the lock
        run.assert_not_called()

    def test_advise_only_dials_authority_and_disables_e2e(self) -> None:
        config = Config()
        config.e2e.enabled = True  # prove the command turns it off
        orchestrator = Mock()
        load_p, log_p = _patches(config)
        with load_p, log_p, patch(
            "issue_orchestrator.infra.repo_lock.is_locked", return_value=False
        ), patch.object(
            cli_triage, "_build_orchestrator", return_value=orchestrator
        ), patch(
            "issue_orchestrator.control.triage_trigger.run_health_review",
            return_value=HealthReviewResult(
                200, launched=True, completed=True, detail="done"
            ),
        ) as run:
            rc = cli_triage.cmd_health_review(_args(advise_only=True, timeout=42.0))

        assert rc == 0
        # A one-shot run must not fire the heavy background E2E suite.
        assert config.e2e.enabled is False
        # --advise-only dials every triage authority to propose.
        authority = config.triage.authority
        assert authority.post_comment == "propose"
        assert authority.create_issue == "propose"
        assert authority.reset_retry == "propose"
        assert authority.kill_hung_session == "propose"
        # The driver ran against the built orchestrator with the CLI timeout...
        run.assert_called_once()
        assert run.call_args.args[0] is orchestrator
        assert run.call_args.kwargs["timeout_s"] == 42.0
        # ...and the one-shot orchestrator was torn down.
        orchestrator.close.assert_called_once()

    def test_default_run_leaves_authority_untouched_but_disables_e2e(self) -> None:
        config = Config()
        config.e2e.enabled = True
        default_authority = config.triage.authority
        orchestrator = Mock()
        load_p, log_p = _patches(config)
        with load_p, log_p, patch(
            "issue_orchestrator.infra.repo_lock.is_locked", return_value=False
        ), patch.object(
            cli_triage, "_build_orchestrator", return_value=orchestrator
        ), patch(
            "issue_orchestrator.control.triage_trigger.run_health_review",
            return_value=HealthReviewResult(
                200, launched=True, completed=False, detail="running"
            ),
        ):
            rc = cli_triage.cmd_health_review(_args(advise_only=False))

        # launched-but-not-completed is still exit 0 (only "not launched" fails).
        assert rc == 0
        assert config.e2e.enabled is False
        assert config.triage.authority is default_authority  # not replaced
        orchestrator.close.assert_called_once()
