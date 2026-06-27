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


def test_check_ai_keys_uses_provider_key_extensions():
    config = Config()
    config.default_agent = DefaultAgentConfig(provider="external-provider")
    config.agents = {"agent:one": _make_agent()}

    ai_key_status = {"EXTERNAL_PROVIDER_API_KEY": (None, "not set")}
    ai_providers = {"EXTERNAL_PROVIDER_API_KEY": {"name": "External Provider"}}
    with (
        patch(
            "issue_orchestrator.infra.ai_keys.get_provider_key_map",
            return_value={"external-provider": "EXTERNAL_PROVIDER_API_KEY"},
        ),
        patch("issue_orchestrator.infra.ai_keys.list_ai_keys", return_value=ai_key_status),
        patch("issue_orchestrator.infra.ai_keys.get_ai_providers", return_value=ai_providers),
    ):
        checks = ai_checks.check_ai_keys(config)

    assert any(
        c.name == "AI Provider Keys"
        and c.status == "warning"
        and "External Provider" in c.detail
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


class _FakeProvider:
    def __init__(
        self,
        name: str,
        executable: str,
        available: bool = True,
        version: str | None = None,
    ):
        self.name = name
        self.executable = executable
        self._available = available
        self._version = version

    def is_available(self) -> bool:
        return self._available

    def check_version(self) -> str | None:
        return self._version


def test_check_ai_provider_clis_reports_actual_executable(monkeypatch):
    monkeypatch.delenv("NVM_BIN", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(
        "issue_orchestrator.infra.provider_cli_diagnostics._find_executable_outside_path",
        lambda executable: [],
    )
    providers = {
        "claude-code": _FakeProvider(
            "claude-code",
            "claude",
            version="2.1.112 (Claude Code)",
        ),
        "codex": _FakeProvider("codex", "codex", available=False),
    }
    monkeypatch.setattr("issue_orchestrator.agent_runner.list_providers", lambda: list(providers))
    monkeypatch.setattr("issue_orchestrator.agent_runner.get_provider", providers.__getitem__)

    checks = ai_checks.check_ai_provider_clis()

    available = next(check for check in checks if check.name == "AI Provider CLIs")
    missing = next(check for check in checks if check.name == "AI Provider CLIs (Missing)")
    assert available.status == "ok"
    assert "claude-code via claude (2.1.112 (Claude Code))" in available.detail
    assert missing.detail == "Not installed: codex; executable 'codex' not found on PATH"


def test_check_ai_provider_clis_reports_expected_executable_when_missing(monkeypatch):
    monkeypatch.setenv("NVM_BIN", "/Users/test/.nvm/versions/node/v24.11.1/bin")
    monkeypatch.setenv("PATH", "/Users/test/.nvm/versions/node/v24.11.1/bin:/usr/bin:/bin")
    monkeypatch.setattr(
        "issue_orchestrator.infra.provider_cli_diagnostics._find_executable_outside_path",
        lambda executable: [Path("/Users/test/.nvm/versions/node/v24.14.1/bin/claude")],
    )
    providers = {
        "claude-code": _FakeProvider("claude-code", "claude", available=False),
    }
    monkeypatch.setattr("issue_orchestrator.agent_runner.list_providers", lambda: list(providers))
    monkeypatch.setattr("issue_orchestrator.agent_runner.get_provider", providers.__getitem__)

    checks = ai_checks.check_ai_provider_clis()

    missing = next(check for check in checks if check.name == "AI Provider CLIs (Missing)")
    assert missing.detail == (
        "Not installed: claude-code (expected executable: claude); "
        "executable 'claude' not found on PATH; "
        "NVM_BIN=/Users/test/.nvm/versions/node/v24.11.1/bin; "
        "found outside PATH: /Users/test/.nvm/versions/node/v24.14.1/bin/claude"
    )
