"""The orchestrator facade's debug-session command (#9012 review r1 F3).

The facade adds only what is its own - the queue cache, read under its lock -
and the launcher launches the session through the terminal the composition
root binds to the same SessionManager that prepared it, down to the session
runner (#9012 rework: the facade no longer relays session starts).
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.control.worktree_manager import get_worktree_path
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.ports.debug_session_launch import DebugSessionStatus


def _checkout(path: Path) -> Path:
    """The issue's worktree: a run is allocated only on an attached branch."""
    path.mkdir(parents=True)
    for command in (
        ["git", "init", "--initial-branch=123-cached"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-m", "base"],
    ):
        result = LocalCommandRunner().run(command, cwd=path, timeout_seconds=30)
        assert result.returncode == 0, result.stderr
    return path


def test_a_cached_issue_launches_its_debug_session_through_the_facade(sample_orchestrator, sample_config) -> None:
    sample_orchestrator.state.cached_queue_issues = [
        Issue(number=123, title="Cached", labels=["agent:web"], repo="test/repo"),
    ]
    _checkout(get_worktree_path(sample_config, 123))

    outcome = sample_orchestrator.launch_debug_session(123)

    assert outcome.status is DebugSessionStatus.LAUNCHED, outcome
    (started,) = sample_orchestrator.deps.runner.plugin.create_session_calls
    assert (started["session_id"], started["session_name"], started["title"]) == (123, "debug-123", "Debug #123")
    assert started["working_dir"] == str(get_worktree_path(sample_config, 123))


def test_an_issue_neither_cached_nor_on_the_host_is_not_found(sample_orchestrator, sample_config) -> None:
    get_worktree_path(sample_config, 123).mkdir(parents=True)

    outcome = sample_orchestrator.launch_debug_session(123)

    assert outcome.status is DebugSessionStatus.ISSUE_NOT_FOUND
    assert sample_orchestrator.deps.runner.plugin.create_session_calls == []
