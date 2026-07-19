"""Unit tests for the domain sandbox scope value object and policy (ADR-0034)."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.sandbox_scope import (
    DEFAULT_SANDBOX_DENY_ENV,
    SandboxScope,
    SandboxScopeContext,
    compute_session_scope,
)


def _agent(*, sandbox: bool, provider: str | None = "claude-code") -> AgentConfig:
    return AgentConfig(
        prompt_path=Path(".prompts/backend.md"),
        prompt_relative=".prompts/backend.md",
        provider=provider,
        model="sonnet",
        sandbox=sandbox,
    )


def _ctx(task_kind: str = "code", worktree: Path = Path("/wt/issue-1")) -> SandboxScopeContext:
    return SandboxScopeContext(task_kind=task_kind, worktree=worktree)


# ---------------------------------------------------------------------------
# SandboxScope value object
# ---------------------------------------------------------------------------


def test_sandbox_scope_is_frozen_value_object() -> None:
    scope = SandboxScope(
        read_roots=(Path("/wt"),),
        write_roots=(Path("/wt"),),
        egress="model-only",
        deny_env=("GITHUB_TOKEN",),
    )
    with pytest.raises((AttributeError, TypeError)):
        scope.egress = "model+web"  # type: ignore[misc]


def test_sandbox_scope_rejects_empty_read_roots() -> None:
    with pytest.raises(ValueError, match="read_roots must not be empty"):
        SandboxScope(read_roots=(), write_roots=(), egress="none", deny_env=())


def test_sandbox_scope_rejects_unknown_egress() -> None:
    with pytest.raises(ValueError, match="egress must be one of"):
        SandboxScope(
            read_roots=(Path("/wt"),),
            write_roots=(),
            egress="everything",  # type: ignore[arg-type]
            deny_env=(),
        )


# ---------------------------------------------------------------------------
# compute_session_scope — opt-in gate
# ---------------------------------------------------------------------------


def test_not_opted_in_returns_none() -> None:
    assert compute_session_scope(_agent(sandbox=False), _ctx()) is None


def test_opted_in_coder_scope() -> None:
    worktree = Path("/wt/issue-42")
    scope = compute_session_scope(_agent(sandbox=True), _ctx("code", worktree))

    assert scope is not None
    assert scope.read_roots == (worktree,)
    assert scope.write_roots == (worktree,)
    assert scope.egress == "model-only"
    assert scope.deny_env == DEFAULT_SANDBOX_DENY_ENV
    assert "GITHUB_TOKEN" in scope.deny_env


def test_opted_in_rework_is_coder_scope() -> None:
    worktree = Path("/wt/issue-7")
    scope = compute_session_scope(_agent(sandbox=True), _ctx("rework", worktree))
    assert scope is not None
    assert scope.write_roots == (worktree,)
    assert scope.egress == "model-only"


@pytest.mark.parametrize("task_kind", ["review", "retrospective-review"])
def test_opted_in_reviewer_scope(task_kind: str) -> None:
    worktree = Path("/wt/issue-9")
    scope = compute_session_scope(_agent(sandbox=True), _ctx(task_kind, worktree))

    assert scope is not None
    # Reviewer reads and writes its own worktree (it runs builds/tests there).
    assert scope.read_roots == (worktree,)
    assert scope.write_roots == (worktree,)
    assert scope.egress == "model-only"


def test_opted_in_triage_is_bounded_not_yolo() -> None:
    # Tech-lead evidence-map read scope is a follow-up; until then triage is
    # still bounded to its own worktree (never left unsandboxed).
    worktree = Path("/wt/issue-3")
    scope = compute_session_scope(_agent(sandbox=True), _ctx("triage", worktree))
    assert scope is not None
    assert scope.read_roots == (worktree,)
    assert scope.write_roots == (worktree,)


def test_unknown_task_kind_fails_safe_to_bounded_scope() -> None:
    worktree = Path("/wt/issue-5")
    scope = compute_session_scope(_agent(sandbox=True), _ctx("mystery", worktree))
    assert scope is not None
    assert scope.write_roots == (worktree,)
    assert scope.egress == "model-only"


def test_opt_in_independent_of_provider() -> None:
    # The scope is provider-agnostic: a codex agent that opts in still gets a
    # scope (the codex *translation* is what is deferred, not the policy).
    worktree = Path("/wt/issue-11")
    scope = compute_session_scope(_agent(sandbox=True, provider="codex"), _ctx("code", worktree))
    assert scope is not None
