"""The improver is a budgeted suite in every mode, off until the operator enables it (#7490)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from issue_orchestrator.domain.budgeted_validation import ValidationCadence
from issue_orchestrator.domain.engine_activity import EngineActivityCadence
from issue_orchestrator.infra.budgeted_validation_config import parse_budgeted_validation

ROOT = Path(__file__).resolve().parents[2]
MODES = sorted((ROOT / ".issue-orchestrator" / "config" / "modes").glob("*/main.yaml"))


def test_every_mode_is_covered() -> None:
    assert len(MODES) == 6


@pytest.mark.parametrize("config", MODES, ids=lambda p: p.parent.name)
def test_the_improver_suite_runs_daily_and_is_off_by_default(config: Path) -> None:
    suites = parse_budgeted_validation(yaml.safe_load(config.read_text())["validation"]["budgeted"])

    improver = suites["tech-lead-improver"]
    assert improver.enabled is False
    assert improver.command == ("make", "tech-lead-improver")
    # Due on engine activity at most daily, never on io merges (#7567); the
    # exam, which tests code, stays on the code-change cadence.
    assert improver.cadence == EngineActivityCadence(max_delay_hours=24)
    assert isinstance(suites["tech-lead-exam"].cadence, ValidationCadence)
    assert improver.timeout_seconds >= 90 * 60 + 600
    assert "tech-lead-exam" in suites


def test_the_make_target_runs_the_improver_on_the_cli_default_agent() -> None:
    makefile = (ROOT / "Makefile").read_text()

    target = makefile.split("tech-lead-improver: sync-deps", 1)[1].split("\n\n", 1)[0]
    assert "cli_tools.improver run" in target
    # By default every engine Control Center runs is audited (#7567).
    assert "--recent-hours $(IMPROVER_RECENT_HOURS)" in target
    # The provider and model are passed only when set, so the CLI's default
    # (the latest improver tournament's winner, #8001) is the one default.
    assert "$(if $(IMPROVER_PROVIDER),--provider $(IMPROVER_PROVIDER),)" in target
    assert "$(if $(IMPROVER_MODEL),--model $(IMPROVER_MODEL),)" in target
    assert "\nIMPROVER_PROVIDER ?=\n" in makefile and "\nIMPROVER_MODEL ?=\n" in makefile
    assert "$(if $(IMPROVER_HEATS),--heats $(IMPROVER_HEATS),)" in target
    assert '--exam-dir "$(EXAM_OUT)"' in target
