"""Unit tests for workspace doctor checks."""

from pathlib import Path

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config, DefaultAgentConfig
from issue_orchestrator.infra.doctor.checks.workspace import check_agents


def _agent_scripts_check(config: Config):
    return next(check for check in check_agents(config) if check.name == "Agent Scripts")


class _FakeProvider:
    def __init__(self, name: str, executable: str, available: bool = True):
        self.name = name
        self.executable = executable
        self._available = available

    def is_available(self) -> bool:
        return self._available


def _patch_providers(monkeypatch, providers: dict[str, _FakeProvider]) -> None:
    def get_provider(name: str) -> _FakeProvider:
        try:
            return providers[name]
        except KeyError as exc:
            raise ValueError(f"Unknown provider: {name!r}") from exc

    monkeypatch.setattr("issue_orchestrator.agent_runner.get_provider", get_provider)


def test_agent_scripts_use_provider_registry_for_provider_agents(
    monkeypatch,
    tmp_path: Path,
):
    config = Config()
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "backend.md",
            provider="claude-code",
        ),
        "agent:reviewer": AgentConfig(
            prompt_path=tmp_path / "reviewer.md",
            provider="codex",
        ),
    }
    _patch_providers(
        monkeypatch,
        {
            "claude-code": _FakeProvider("claude-code", "claude"),
            "codex": _FakeProvider("codex", "codex"),
        },
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.checks.workspace.shutil.which",
        lambda _: None,
    )

    check = _agent_scripts_check(config)

    assert check.status == "ok"
    assert check.detail == "All found"


def test_agent_scripts_report_missing_configured_provider_cli(
    monkeypatch,
    tmp_path: Path,
):
    config = Config()
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "backend.md",
            provider="claude-code",
        ),
    }
    _patch_providers(
        monkeypatch,
        {"claude-code": _FakeProvider("claude-code", "claude", available=False)},
    )

    check = _agent_scripts_check(config)

    assert check.status == "error"
    assert check.detail == "Missing: agent:backend: claude-code (expected executable: claude)"


def test_agent_scripts_use_default_agent_provider(monkeypatch, tmp_path: Path):
    config = Config()
    config.default_agent = DefaultAgentConfig(provider="codex")
    config.agents = {
        "agent:reviewer": AgentConfig(prompt_path=tmp_path / "reviewer.md"),
    }
    _patch_providers(monkeypatch, {"codex": _FakeProvider("codex", "codex")})
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.checks.workspace.shutil.which",
        lambda _: None,
    )

    check = _agent_scripts_check(config)

    assert check.status == "ok"
    assert check.detail == "All found"


def test_agent_scripts_still_validate_legacy_commands(monkeypatch, tmp_path: Path):
    config = Config()
    config.agents = {
        "agent:legacy": AgentConfig(
            prompt_path=tmp_path / "legacy.md",
            command="missing-agent --do-work",
        ),
    }
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.checks.workspace.shutil.which",
        lambda _: None,
    )

    check = _agent_scripts_check(config)

    assert check.status == "error"
    assert check.detail == "Missing: agent:legacy: missing-agent"
