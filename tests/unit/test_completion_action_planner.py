"""Direct tests for completion action planning policy."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from issue_orchestrator.control.actions import AddCommentAction, AddLabelAction, RemoveLabelAction
from issue_orchestrator.control.completion_action_planner import (
    CompletionActionPlanner,
    critical_processing_errors,
)
from issue_orchestrator.control.completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUSH,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, Session, SessionStatus
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.triage_manifest import PRToReview, TriageManifest
from issue_orchestrator.domain.triage_session import (
    TRIAGE_ASSIGNMENT_FILENAME,
    TriageAssignment,
    TriageSessionFlavor,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import RepositoryHost
from tests.unit.session_run_helpers import make_session_run_assets


def make_issue(
    number: int = 1,
    *,
    labels: list[str] | None = None,
) -> Issue:
    """Create an issue for planner tests."""
    return Issue(
        number=number,
        title=f"Test issue {number}",
        labels=labels or ["agent:test"],
        repo="owner/repo",
    )


def make_session(
    tmp_path: Path,
    *,
    issue: Issue | None = None,
    terminal_id: str = "issue-1",
) -> Session:
    """Create a session for planner tests."""
    issue = issue or make_issue()
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue.number)), task=TaskKind.CODE),
        issue=issue,
        agent_config=AgentConfig(prompt_path=tmp_path / "prompt.md", timeout_minutes=45),
        terminal_id=terminal_id,
        worktree_path=tmp_path,
        branch_name=f"issue-{issue.number}",
        run_assets=make_session_run_assets(tmp_path, session_name=terminal_id),
    )


def make_planner(config: Config, *, issue_labels: list[str] | None = None) -> CompletionActionPlanner:
    """Create a planner with a repository host that can answer label reads."""
    issue = SimpleNamespace(labels=issue_labels or [])
    repository_host = cast(RepositoryHost, SimpleNamespace(get_issue=lambda _number: issue))
    return CompletionActionPlanner(config, repository_host, LabelManager(config))


def added_labels(actions: tuple[object, ...]) -> set[str]:
    """Return labels added by a planner result."""
    return {action.label for action in actions if isinstance(action, AddLabelAction)}


def removed_labels(actions: tuple[object, ...]) -> set[str]:
    """Return labels removed by a planner result."""
    return {action.label for action in actions if isinstance(action, RemoveLabelAction)}


def comments(actions: tuple[object, ...]) -> list[str]:
    """Return comments emitted by a planner result."""
    return [action.comment for action in actions if isinstance(action, AddCommentAction)]


def test_timeout_issue_session_marks_blocked_failed_and_releases_claim(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.TIMED_OUT,
    )

    assert "blocked-failed" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Timed Out" in comment for comment in comments(actions))


def test_failed_issue_session_without_retry_needs_human(tmp_path: Path) -> None:
    config = Config()
    config.retry.interrupted_sessions.enabled = False

    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.FAILED,
    )

    assert "needs-human" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Needs Investigation" in comment for comment in comments(actions))


def test_blocked_issue_session_uses_reported_label_and_reason(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.BLOCKED,
        blocked_label="blocked-upstream",
        blocked_reason="Waiting on dependency",
    )

    assert "blocked-upstream" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Waiting on dependency" in comment for comment in comments(actions))


def test_completed_with_publish_error_tracks_publish_failure(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        processing_errors=[f"{ERROR_PREFIX_PUSH}: rejected"],
        diagnostic_path=".issue-orchestrator/diagnostics/publish.md",
    )

    assert {"publish-failed", "publish-fail-count-1"} <= added_labels(actions)
    assert {"in-progress", "needs-rework"} <= removed_labels(actions)
    assert any("Publishing Failed" in comment for comment in comments(actions))


def test_review_exchange_halt_puts_issue_on_hold(tmp_path: Path) -> None:
    config = Config()
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        review_exchange_halted=True,
    )

    assert "blocked-failed" in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Review Exchange Halted" in comment for comment in comments(actions))


def _make_triage_config() -> Config:
    config = Config()
    config.triage_review_agent = "agent:triage"
    config.triage_reviewed_label = "triage-reviewed"
    return config


def _write_triage_run(
    tmp_path: Path,
    *,
    with_manifest: bool = True,
    assignment: TriageAssignment | None = None,
) -> Path:
    """Write a session run dir with optional triage manifest and assignment."""
    run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    run_manifest: dict[str, str] = {}
    if with_manifest:
        manifest = TriageManifest(
            prs=[
                PRToReview(number=101, title="PR 101", url="https://example/pr/101", branch="b1"),
                PRToReview(number=102, title="PR 102", url="https://example/pr/102", branch="b2"),
            ]
        )
        manifest_path = tmp_path / "triage-manifest.json"
        manifest.write(manifest_path)
        run_manifest["triage_manifest"] = str(manifest_path)
    if assignment is not None:
        assignment_path = run_dir / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME
        assignment.write(assignment_path)
        run_manifest["triage_assignment"] = str(assignment_path)
    (run_dir / "manifest.json").write_text(json.dumps(run_manifest))
    return run_dir


def _triage_labels(actions: tuple[object, ...]) -> list[AddLabelAction]:
    return [
        action for action in actions
        if isinstance(action, AddLabelAction) and action.label == "triage-reviewed"
    ]


def test_completed_triage_session_labels_manifest_prs(tmp_path: Path) -> None:
    config = _make_triage_config()
    session = make_session(tmp_path, issue=make_issue(labels=["agent:triage"]))
    _write_triage_run(
        tmp_path,
        assignment=TriageAssignment(flavor=TriageSessionFlavor.BATCH_REVIEW),
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert {action.issue_number for action in _triage_labels(actions)} == {101, 102}
    assert "in-progress" in removed_labels(actions)


def test_failure_investigation_triage_session_never_labels_manifest_prs(
    tmp_path: Path,
) -> None:
    """A focused investigation must not label PRs even when a manifest exists (#6768 B4)."""
    config = _make_triage_config()
    session = make_session(tmp_path, issue=make_issue(labels=["agent:triage"]))
    _write_triage_run(
        tmp_path,
        assignment=TriageAssignment(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            focus_issue_number=1,
            focus_reason="Investigate: timed out",
        ),
    )

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _triage_labels(actions) == []
    assert "in-progress" in removed_labels(actions)


def test_triage_session_without_assignment_skips_labels_and_warns(
    tmp_path: Path, caplog
) -> None:
    """Pre-upgrade sessions fail safe: no labels, PRs re-enter the next batch."""
    config = _make_triage_config()
    session = make_session(tmp_path, issue=make_issue(labels=["agent:triage"]))
    _write_triage_run(tmp_path, assignment=None)

    actions = make_planner(config).generate_completion_actions(
        session,
        SessionStatus.COMPLETED,
    )

    assert _triage_labels(actions) == []
    assert "in-progress" in removed_labels(actions)
    assert "No triage assignment" in caplog.text


def test_interrupted_retry_adds_guard_and_keeps_retry_loop_bounded(tmp_path: Path) -> None:
    config = Config()
    config.retry.interrupted_sessions.enabled = True
    actions = make_planner(config).generate_completion_actions(
        make_session(tmp_path),
        SessionStatus.FAILED,
    )

    assert config.retry.interrupted_sessions.coding_guard_label in added_labels(actions)
    assert "in-progress" in removed_labels(actions)
    assert any("Session Interrupted" in comment for comment in comments(actions))


def test_create_pr_error_is_downgraded_when_pr_exists(caplog) -> None:
    critical, downgraded = critical_processing_errors(
        [f"{ERROR_PREFIX_CREATE_PR}: 422 already exists"],
        pr_url="https://github.com/owner/repo/pull/5",
        issue_number=5,
        log_downgraded=True,
        context="test",
    )

    assert critical == []
    assert downgraded == [f"{ERROR_PREFIX_CREATE_PR}: 422 already exists"]
    assert "Ignoring non-blocking create_pr processing errors" in caplog.text
