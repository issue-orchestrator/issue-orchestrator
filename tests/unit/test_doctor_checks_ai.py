"""Unit tests for doctor AI provider checks."""

from pathlib import Path
from unittest.mock import patch

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config, DefaultAgentConfig
from issue_orchestrator.infra.doctor.checks import ai as ai_checks


def _make_agent(provider: str | None = None, command: str | None = None) -> AgentConfig:
    cfg = AgentConfig(prompt_path=Path("prompt.md"))
    if provider is not None:
        cfg.provider = provider
    if command is not None:
        cfg.command = command
    return cfg


def test_check_ai_keys_cli_providers_no_keys_required():
    config = Config()
    config.default_agent = DefaultAgentConfig(provider="claude-code")
    config.agents = {"agent:one": _make_agent()}

    with patch("issue_orchestrator.infra.ai_keys.list_ai_keys", return_value={}):
        checks = ai_checks.check_ai_keys(config)

    assert any(
        c.name == "AI Provider Keys" and c.status == "info" and "No API keys required" in c.detail
        for c in checks
    )


def test_check_ai_keys_warns_when_required_missing():
    config = Config()
    config.default_agent = DefaultAgentConfig(provider="openai")
    config.agents = {"agent:one": _make_agent()}

    ai_key_status = {"OPENAI_API_KEY": (None, "not set")}
    with patch("issue_orchestrator.infra.ai_keys.list_ai_keys", return_value=ai_key_status):
        checks = ai_checks.check_ai_keys(config)

    assert any(
        c.name == "AI Provider Keys" and c.status == "warning" and "OpenAI" in c.detail
        for c in checks
    )


def test_check_ai_keys_reports_unknown_provider():
    config = Config()
    config.agents = {"agent:one": _make_agent(provider="mystery-ai")}

    with patch("issue_orchestrator.infra.ai_keys.list_ai_keys", return_value={}):
        checks = ai_checks.check_ai_keys(config)

    assert any(
        c.name == "AI Provider Keys (Unknown Providers)" and "mystery-ai" in c.detail
        for c in checks
    )
