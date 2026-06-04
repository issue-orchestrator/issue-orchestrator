"""Direct tests for session completion diagnostics."""

from pathlib import Path

from issue_orchestrator.control.session_completion_diagnostics import surface_failure_context
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, Session, SessionStatus
from issue_orchestrator.domain.session_key import SessionKey, TaskKind


def _session(tmp_path: Path, *, permission_mode: str = "bypassPermissions") -> Session:
    provider_args = (
        {"permission_mode": permission_mode} if permission_mode != "default" else {}
    )
    return Session(
        key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
        issue=Issue(123, "Test issue", ["agent:test"], repo="owner/repo"),
        agent_config=AgentConfig(
            prompt_path=tmp_path / "prompt.md",
            provider_args=provider_args,
            command="claude -p prompt",
        ),
        terminal_id="issue-123",
        worktree_path=tmp_path,
        branch_name="issue-123-test",
        agent_label="agent:test",
    )


def test_surface_failure_context_warns_about_default_permission_mode(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(
        "issue_orchestrator.adapters.session_log.registry.get_log_provider",
        lambda _ai_system: None,
    )

    surface_failure_context(
        _session(tmp_path, permission_mode="default"),
        SessionStatus.FAILED,
    )

    assert "permission_mode is 'default'" in caplog.text
    assert "provider_args.permission_mode" in caplog.text
    assert "Add 'permission_mode" not in caplog.text
    assert "No detailed failure context available" in caplog.text


def test_surface_failure_context_reports_missing_log_file(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    class MissingLogProvider:
        def get_log_path(self, _worktree_path: Path, _terminal_id: str) -> None:
            return None

    monkeypatch.setattr(
        "issue_orchestrator.adapters.session_log.registry.get_log_provider",
        lambda _ai_system: MissingLogProvider(),
    )

    surface_failure_context(_session(tmp_path), SessionStatus.TIMED_OUT)

    assert "Log file: NOT FOUND" in caplog.text
    assert "No detailed failure context available" in caplog.text


def test_surface_failure_context_includes_provider_context(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    log_path = tmp_path / "session.log"

    class ContextProvider:
        def get_log_path(self, _worktree_path: Path, _terminal_id: str) -> Path:
            return log_path

        def get_failure_context(self, path: Path) -> str:
            assert path == log_path
            return "provider-specific failure context"

    monkeypatch.setattr(
        "issue_orchestrator.adapters.session_log.registry.get_log_provider",
        lambda _ai_system: ContextProvider(),
    )

    surface_failure_context(_session(tmp_path), SessionStatus.FAILED)

    assert f"Log file: {log_path}" in caplog.text
    assert "provider-specific failure context" in caplog.text
