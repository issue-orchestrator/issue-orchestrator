"""Tests for the forbidden-on-branch runtime-artifact guard (#6659).

This guard is distinct from the dirty-tree filters: it answers "is this path,
present in a *committed* branch diff against base, a runtime artifact that must
never have entered the branch?". Runtime outputs under ``.issue-orchestrator/``
(review-exchange prompts, persistent-pair recordings, validation records, …)
break the reviewer-worktree fast-forward checkout when committed, so they must
be rejected before publish/review-exchange finalization. Project-owned config is
allowlisted and must pass.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.infra.runtime_artifacts import (
    build_forbidden_runtime_artifact_reason,
    forbidden_branch_runtime_artifacts,
    is_forbidden_branch_runtime_artifact,
)


# ---------------------------------------------------------------------------
# is_forbidden_branch_runtime_artifact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".issue-orchestrator/persistent-pairs/issue-6594/coder/terminal-recording.jsonl",
        ".issue-orchestrator/persistent-pairs/issue-6594/validation-record.json",
        ".issue-orchestrator/review-feedback/cycle-1.md",
        ".issue-orchestrator/review-response.json",
        ".issue-orchestrator/review-report.md",
        ".issue-orchestrator/review-exchange-turn-prompt.md",
        ".issue-orchestrator/tool-homes/codex/config.toml",
        # A runtime output we have not enumerated explicitly is still caught
        # because the guard is allowlist-based, not denylist-based.
        ".issue-orchestrator/some-future-runtime-output.json",
    ],
)
def test_runtime_outputs_are_forbidden(path: str) -> None:
    assert is_forbidden_branch_runtime_artifact(path) is True


@pytest.mark.parametrize(
    "path",
    [
        ".issue-orchestrator/config/main.yaml",
        ".issue-orchestrator/config/hooks-validate.yaml",
        ".issue-orchestrator/runtime-ignore",
        ".issue-orchestrator/allow-no-verify-dry-run",
    ],
)
def test_tracked_project_files_are_allowed(path: str) -> None:
    assert is_forbidden_branch_runtime_artifact(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "src/issue_orchestrator/control/completion_processor.py",
        "README.md",
        # ``.claude/`` is legitimately tracked (hooks, settings, skills) so it
        # is intentionally outside the branch guard's roots.
        ".claude/settings.json",
        ".claude/hooks/pre_push.py",
    ],
)
def test_non_guarded_paths_are_allowed(path: str) -> None:
    assert is_forbidden_branch_runtime_artifact(path) is False


def test_leading_slash_and_backslash_are_normalized() -> None:
    assert is_forbidden_branch_runtime_artifact(
        "/.issue-orchestrator/review-response.json"
    )
    assert is_forbidden_branch_runtime_artifact(
        ".issue-orchestrator\\review-feedback\\cycle-1.md"
    )


# ---------------------------------------------------------------------------
# forbidden_branch_runtime_artifacts
# ---------------------------------------------------------------------------


def test_forbidden_filter_dedupes_and_sorts() -> None:
    paths = [
        ".issue-orchestrator/review-response.json",
        ".issue-orchestrator/config/main.yaml",
        ".issue-orchestrator/persistent-pairs/issue-1/coder/rec.jsonl",
        ".issue-orchestrator/review-response.json",
        "src/main.py",
    ]
    assert forbidden_branch_runtime_artifacts(paths) == [
        ".issue-orchestrator/persistent-pairs/issue-1/coder/rec.jsonl",
        ".issue-orchestrator/review-response.json",
    ]


def test_forbidden_filter_empty_when_only_allowlisted() -> None:
    assert forbidden_branch_runtime_artifacts(
        [".issue-orchestrator/config/main.yaml", "src/app.py"]
    ) == []


# ---------------------------------------------------------------------------
# build_forbidden_runtime_artifact_reason
# ---------------------------------------------------------------------------


def test_reason_lists_paths_and_is_actionable() -> None:
    reason = build_forbidden_runtime_artifact_reason(
        [".issue-orchestrator/review-response.json"]
    )
    assert "runtime artifacts" in reason
    assert "git rm --cached" in reason
    assert ".issue-orchestrator/review-response.json" in reason


def test_reason_truncates_long_lists() -> None:
    paths = [f".issue-orchestrator/persistent-pairs/issue-{i}/rec.jsonl" for i in range(12)]
    reason = build_forbidden_runtime_artifact_reason(paths)
    assert "(+4 more)" in reason
