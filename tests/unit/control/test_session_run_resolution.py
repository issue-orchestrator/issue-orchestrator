"""Tests for deterministic session run artifact resolution."""

from pathlib import Path
from unittest.mock import Mock

from issue_orchestrator.control.completion_observer import CompletionObserver
from issue_orchestrator.control.session_run_resolution import resolve_session_run_dir
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    Session,
    SessionKey,
    TaskKind,
)
from issue_orchestrator.infra.provider_resilience import (
    ProviderStatus,
    now_iso,
    write_provider_status,
)
from issue_orchestrator.ports.provider_resilience import ProviderErrorType
from issue_orchestrator.ports.session_output import SessionOutput


def _session(
    tmp_path: Path,
    run_dir: Path | None,
    *,
    completion_path: str = ".issue-orchestrator/completion.json",
) -> Session:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("prompt", encoding="utf-8")
    issue = Issue(number=123, title="Test issue", labels=["agent:test"])
    return Session(
        key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
        issue=issue,
        agent_config=AgentConfig(prompt_path=prompt_path, model="sonnet"),
        terminal_id="issue-123",
        worktree_path=tmp_path / "worktree",
        branch_name="123-test",
        completion_path=completion_path,
        run_dir=run_dir,
    )


def test_recorded_run_dir_is_authoritative(tmp_path: Path) -> None:
    run_dir = (
        tmp_path
        / "worktree"
        / ".issue-orchestrator"
        / "sessions"
        / "20260525__coding-1"
    )
    run_dir.mkdir(parents=True)
    session = _session(tmp_path, run_dir)
    session_output = Mock(spec=SessionOutput)

    resolved = resolve_session_run_dir(session_output, session)

    assert resolved == run_dir
    session_output.find_run_dir.assert_not_called()
    session_output.read_manifest.assert_not_called()


def test_missing_recorded_run_dir_still_prevents_discovery_fallback(
    tmp_path: Path,
) -> None:
    run_dir = (
        tmp_path
        / "worktree"
        / ".issue-orchestrator"
        / "sessions"
        / "20260525__coding-1"
    )
    session = _session(tmp_path, run_dir)
    session_output = Mock(spec=SessionOutput)

    resolved = resolve_session_run_dir(session_output, session)

    assert resolved == run_dir
    session_output.find_run_dir.assert_not_called()
    session_output.read_manifest.assert_not_called()


def test_legacy_session_uses_manifest_checked_fallback(tmp_path: Path) -> None:
    run_dir = (
        tmp_path
        / "worktree"
        / ".issue-orchestrator"
        / "sessions"
        / "20260525__coding-1"
    )
    session = _session(tmp_path, run_dir=None)
    session_output = Mock(spec=SessionOutput)
    session_output.find_run_dir.side_effect = [None, run_dir]
    session_output.read_manifest.return_value = {"issue_number": 123}

    resolved = resolve_session_run_dir(session_output, session)

    assert resolved == run_dir
    assert session_output.find_run_dir.call_args_list[0].args == (
        session.worktree_path,
        session.terminal_id,
    )
    assert session_output.find_run_dir.call_args_list[1].args == (
        session.worktree_path,
    )


def test_legacy_session_prefers_completion_path_session_name(
    tmp_path: Path,
) -> None:
    run_dir = (
        tmp_path
        / "worktree"
        / ".issue-orchestrator"
        / "sessions"
        / "20260525__coding-1"
    )
    session = _session(
        tmp_path,
        run_dir=None,
        completion_path=".issue-orchestrator/sessions/coding-1/completion-agent_backend.json",
    )
    session_output = Mock(spec=SessionOutput)
    session_output.session_name_from_path.return_value = "coding-1"
    session_output.find_run_dir.return_value = run_dir

    resolved = resolve_session_run_dir(session_output, session)

    assert resolved == run_dir
    session_output.find_run_dir.assert_called_once_with(
        session.worktree_path,
        "coding-1",
    )


def test_completion_observer_reads_provider_status_from_recorded_run_dir(
    tmp_path: Path,
) -> None:
    run_dir = (
        tmp_path
        / "worktree"
        / ".issue-orchestrator"
        / "sessions"
        / "20260525__coding-1"
    )
    write_provider_status(
        run_dir,
        ProviderStatus(
            provider="codex",
            error_type=ProviderErrorType.TRANSIENT,
            attempts=3,
            succeeded=False,
            exit_code=1,
            timed_out=False,
            last_error_summary="provider unavailable",
            last_attempt_at=now_iso(),
        ),
    )
    session = _session(tmp_path, run_dir)
    session_output = Mock(spec=SessionOutput)
    observer = CompletionObserver(session_output=session_output)

    provider_status = observer._read_provider_status(session)  # noqa: SLF001

    assert provider_status is not None
    assert provider_status.provider == "codex"
    session_output.find_run_dir.assert_not_called()
