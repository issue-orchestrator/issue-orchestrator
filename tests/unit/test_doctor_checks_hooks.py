"""Unit tests for doctor hook checks."""

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks import hooks as hook_checks


def test_check_hook_verification_no_agents_reports_ai_agent_check():
    config = Config()
    config.agents = {}

    checks = hook_checks.check_hook_verification(config)

    assert any(
        c.name == "AI Agent Hooks (Installation)" and c.status == "warning"
        for c in checks
    )
