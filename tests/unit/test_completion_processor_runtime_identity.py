"""Runtime identity audit coverage for completion PR creation."""

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

from issue_orchestrator.control.completion_processor import (
    CompletionProcessor,
    GitAdapter,
    LabelAdapter,
    PRAdapter,
)
from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.domain.runtime_identity import RuntimeIdentity
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.working_copy import DiffResult, PushResult


def _make_git_adapter() -> Mock:
    adapter = Mock(spec=GitAdapter)
    adapter.push = Mock(
        return_value=PushResult(
            success=True,
            branch="issue-123",
            remote="origin",
            message="Pushed",
        )
    )
    adapter.rebase_on_branch = Mock(return_value=MagicMock(success=True))
    adapter.create_branch_from_current = Mock()
    adapter.list_branch_names = Mock(return_value=["issue-123"])
    adapter.get_current_branch = Mock(return_value="issue-123")
    adapter.has_uncommitted_changes = Mock(return_value=False)
    adapter.has_tracked_changes = Mock(return_value=False)
    adapter.list_dirty_files = Mock(return_value=[])
    adapter.diff_against_base = Mock(return_value=DiffResult(success=True, diff_text=""))
    return adapter


def _make_pr_adapter() -> Mock:
    adapter = Mock(spec=PRAdapter)
    adapter.get_prs_for_issue = Mock(return_value=[])
    adapter.get_prs_for_branch = Mock(return_value=[])
    adapter.create_pr = Mock(
        return_value=PRInfo(
            number=42,
            title="Test PR",
            url="https://github.com/owner/repo/pull/42",
            branch="issue-123",
            body="Test body",
            state="open",
            labels=[],
        )
    )
    adapter.add_comment = Mock(return_value="comment-id")
    return adapter


def _write_completion(worktree: Path, record: CompletionRecord) -> None:
    record_dir = worktree / ".issue-orchestrator"
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / "completion.json").write_text(
        json.dumps(record.to_dict()),
        encoding="utf-8",
    )
    (record_dir / "sessions" / record.session_id).mkdir(parents=True)


def test_completion_processor_stamps_runtime_identity_on_created_pr(
    tmp_path: Path,
) -> None:
    label_adapter = Mock(spec=LabelAdapter)
    pr_adapter = _make_pr_adapter()
    git_adapter = _make_git_adapter()
    processor = CompletionProcessor(
        label_adapter=label_adapter,
        pr_adapter=pr_adapter,
        git_adapter=git_adapter,
        session_output=FileSystemSessionOutput(),
        runtime_identity=RuntimeIdentity(
            package_version="1.2.3",
            source_commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        ),
    )
    record = CompletionRecord(
        session_id="test-session",
        timestamp="2026-05-27T00:00:00+00:00",
        outcome=CompletionOutcome.COMPLETED,
        summary="Implemented feature",
        requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        implementation="Added the feature",
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_completion(worktree, record)

    result = processor.process(worktree, issue_number=123, issue_title="Add feature")

    assert result.success
    body = pr_adapter.create_pr.call_args.kwargs["body"]
    assert "## Orchestration Audit" in body
    assert "| Orchestrator version | `1.2.3` |" in body
    assert "| Orchestrator commit | `abcdef1234567890abcdef1234567890abcdef12` |" in body
